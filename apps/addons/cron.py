import array
import itertools
import logging
import operator
import os
import subprocess
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connections, transaction
from django.db.models import Q, F, Avg, Count

import multidb
import path
import recommend
from celery.messaging import establish_connection
from celeryutils import task

import amo
import cronjobs
from amo.utils import chunked
from addons import search
from addons.models import Addon, FrozenAddon, AppSupport
from addons.utils import ReverseNameLookup
from files.models import File
from stats.models import UpdateCount
from translations.models import Translation

log = logging.getLogger('z.cron')
task_log = logging.getLogger('z.task')
recs_log = logging.getLogger('z.recs')


@cronjobs.register
def build_reverse_name_lookup():
    """Builds a Reverse Name lookup table in REDIS."""
    ReverseNameLookup().clear()

    # Get all add-on name ids
    names = (Addon.objects.filter(
        name__isnull=False, type__in=[amo.ADDON_EXTENSION, amo.ADDON_THEME])
        .values_list('name_id', 'id'))

    for chunk in chunked(names, 100):
        _build_reverse_name_lookup.delay(dict(chunk))


@task
def _build_reverse_name_lookup(names, **kw):
    clear = kw.get('clear', False)
    translations = (Translation.objects.filter(id__in=names)
                    .values_list('id', 'localized_string'))

    if clear:
        for addon_id in names.values():
            ReverseNameLookup().delete(addon_id)

    for t_id, string in translations:
        if string:
            ReverseNameLookup().add(string, names[t_id])


@cronjobs.register
def fast_current_version():
    # Only find the really recent versions; this is called a lot.
    t = datetime.now() - timedelta(minutes=5)
    qs = Addon.objects.values_list('id')
    q1 = qs.filter(status=amo.STATUS_PUBLIC,
                   versions__files__datestatuschanged__gte=t)
    q2 = qs.filter(status__in=amo.UNREVIEWED_STATUSES,
                   versions__files__created__gte=t)
    addons = set(q1) | set(q2)
    if addons:
        _update_addons_current_version(addons)


#TODO(davedash): This will not be needed as a cron task after remora.
@cronjobs.register
def update_addons_current_version():
    """Update the current_version field of the addons."""
    d = (Addon.objects.filter(disabled_by_user=False,
                              status__in=amo.VALID_STATUSES)
         .exclude(type=amo.ADDON_PERSONA).values_list('id'))

    with establish_connection() as conn:
        for chunk in chunked(d, 100):
            _update_addons_current_version.apply_async(args=[chunk],
                                                       connection=conn)


@task(rate_limit='20/m')
def _update_addons_current_version(data, **kw):
    task_log.info("[%s@%s] Updating addons current_versions." %
                   (len(data), _update_addons_current_version.rate_limit))
    for pk in data:
        try:
            addon = Addon.objects.get(pk=pk[0])
            addon.update_version()
        except Addon.DoesNotExist:
            m = "Failed to update current_version. Missing add-on: %d" % (pk)
            task_log.debug(m)
    transaction.commit_unless_managed()


@cronjobs.register
def update_addon_average_daily_users():
    """Update add-ons ADU totals."""
    cursor = connections[multidb.get_slave()].cursor()
    # We need to use SQL for this until
    # http://code.djangoproject.com/ticket/11003 is resolved
    q = """SELECT
               addon_id, AVG(`count`)
           FROM update_counts
           USE KEY (`addon_and_count`)
           WHERE `date` >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
           GROUP BY addon_id
           ORDER BY addon_id"""
    cursor.execute(q)
    d = cursor.fetchall()
    cursor.close()

    with establish_connection() as conn:
        for chunk in chunked(d, 1000):
            _update_addon_average_daily_users.apply_async(args=[chunk],
                                                          connection=conn)


@task(rate_limit='15/m')
def _update_addon_average_daily_users(data, **kw):
    task_log.info("[%s@%s] Updating add-ons ADU totals." %
                   (len(data), _update_addon_average_daily_users.rate_limit))

    for pk, count in data:
        Addon.objects.filter(pk=pk).update(average_daily_users=count)


@cronjobs.register
def update_addon_download_totals():
    """Update add-on total and average downloads."""
    cursor = connections[multidb.get_slave()].cursor()
    # We need to use SQL for this until
    # http://code.djangoproject.com/ticket/11003 is resolved
    q = """SELECT
               addon_id, AVG(count), SUM(count)
           FROM download_counts
           USE KEY (`addon_and_count`)
           GROUP BY addon_id
           ORDER BY addon_id"""
    cursor.execute(q)
    d = cursor.fetchall()
    cursor.close()

    with establish_connection() as conn:
        for chunk in chunked(d, 1000):
            _update_addon_download_totals.apply_async(args=[chunk],
                                                      connection=conn)


@task(rate_limit='15/m')
def _update_addon_download_totals(data, **kw):
    task_log.info("[%s@%s] Updating add-ons download+average totals." %
                   (len(data), _update_addon_download_totals.rate_limit))

    for pk, avg, sum in data:
        Addon.objects.filter(pk=pk).update(average_daily_downloads=avg,
                                           total_downloads=sum)


def _change_last_updated(next):
    # We jump through some hoops here to make sure we only change the add-ons
    # that really need it, and to invalidate properly.
    current = dict(Addon.objects.values_list('id', 'last_updated'))
    changes = {}

    for addon, last_updated in next.items():
        if current[addon] != last_updated:
            changes[addon] = last_updated

    if not changes:
        return

    log.debug('Updating %s add-ons' % len(changes))
    # Update + invalidate.
    for addon in Addon.uncached.filter(id__in=changes).no_transforms():
        addon.last_updated = changes[addon.id]
        addon.save()


@cronjobs.register
def addon_last_updated():
    next = {}
    for q in Addon._last_updated_queries().values():
        for addon, last_updated in q.values_list('id', 'last_updated'):
            next[addon] = last_updated

    _change_last_updated(next)

    # Get anything that didn't match above.
    other = (Addon.uncached.filter(last_updated__isnull=True)
             .values_list('id', 'created'))
    _change_last_updated(dict(other))


@cronjobs.register
def update_addon_appsupport():
    # Find all the add-ons that need their app support details updated.
    newish = (Q(last_updated__gte=F('appsupport__created')) |
              Q(appsupport__created__isnull=True))
    # Search providers don't list supported apps.
    has_app = Q(versions__apps__isnull=False) | Q(type=amo.ADDON_SEARCH)
    has_file = (Q(status=amo.STATUS_LISTED) |
                Q(versions__files__status__in=amo.VALID_STATUSES))
    good = Q(has_app, has_file) | Q(type=amo.ADDON_PERSONA)
    ids = (Addon.objects.valid().no_cache().distinct()
           .filter(newish, good).values_list('id', flat=True))

    with establish_connection() as conn:
        for chunk in chunked(ids, 20):
            _update_appsupport.apply_async(args=[chunk], connection=conn)


@cronjobs.register
def update_all_appsupport():
    from .tasks import update_appsupport
    ids = sorted(set(AppSupport.objects.values_list('addon', flat=True)))
    task_log.info('Updating appsupport for %s addons.' % len(ids))
    for idx, chunk in enumerate(chunked(ids, 100)):
        if idx % 10 == 0:
            task_log.info('[%s/%s] Updating appsupport.'
                          % (idx * 100, len(ids)))
        update_appsupport(chunk)


@task(rate_limit='30/m')
@transaction.commit_manually
def _update_appsupport(ids, **kw):
    from .tasks import update_appsupport
    update_appsupport(ids)


@cronjobs.register
def addons_add_slugs():
    """Give slugs to any slugless addons."""
    Addon._meta.get_field('modified').auto_now = False
    q = Addon.objects.filter(slug=None).order_by('id')
    ids = q.values_list('id', flat=True)

    cnt = 0
    total = len(ids)
    task_log.info('%s addons without slugs' % total)
    # Chunk it so we don't do huge queries.
    for chunk in chunked(ids, 300):
        # Slugs are set in Addon.__init__.
        list(q.no_cache().filter(id__in=chunk))
        cnt += 300
        task_log.info('Slugs added to %s/%s add-ons.' % (cnt, total))


@cronjobs.register
def hide_disabled_files():
    # If an add-on or a file is disabled, it should be moved to
    # GUARDED_ADDONS_PATH so it's not publicly visible.
    q = (Q(version__addon__status=amo.STATUS_DISABLED)
         | Q(version__addon__disabled_by_user=True))
    ids = (File.objects.filter(q | Q(status=amo.STATUS_DISABLED))
           .values_list('id', flat=True))
    for chunk in chunked(ids, 300):
        qs = File.uncached.filter(id__in=chunk).select_related('version')
        for f in qs:
            f.hide_disabled_file()


@cronjobs.register
def unhide_disabled_files():
    # Files are getting stuck in /guarded-addons for some reason. This job
    # makes sure guarded add-ons are supposed to be disabled.
    log = logging.getLogger('z.files.disabled')
    q = (Q(version__addon__status=amo.STATUS_DISABLED)
         | Q(version__addon__disabled_by_user=True))
    files = set(File.objects.filter(q | Q(status=amo.STATUS_DISABLED))
                .values_list('version__addon', 'filename'))
    for filepath in path.path(settings.GUARDED_ADDONS_PATH).walkfiles():
        addon, filename = filepath.split('/')[-2:]
        if tuple([int(addon), filename]) not in files:
            log.warning('File that should not be guarded: %s.' % filepath)
            try:
                file_ = (File.objects.select_related('version__addon')
                         .get(version__addon=addon, filename=filename))
                file_.unhide_disabled_file()
                if (file_.version.addon.status in amo.MIRROR_STATUSES
                    and file_.status in amo.MIRROR_STATUSES):
                    file_.copy_to_mirror()
            except File.DoesNotExist:
                log.warning('File does not exist: %s.' % filepath)
            except Exception:
                log.error('Could not unhide file: %s.' % filepath,
                          exc_info=True)


@cronjobs.register
def deliver_hotness():
    """
    Calculate hotness of all add-ons.

    a = avg(users this week)
    b = avg(users three weeks before this week)
    hotness = (a-b) / b if a > 1000 and b > 1 else 0
    """
    frozen = [f.id for f in FrozenAddon.objects.all()]
    all_ids = list((Addon.objects.exclude(type=amo.ADDON_PERSONA)
                   .values_list('id', flat=True)))
    now = datetime.now()
    one_week = now - timedelta(days=7)
    four_weeks = now - timedelta(days=28)
    for ids in chunked(all_ids, 300):
        addons = Addon.uncached.filter(id__in=ids).no_transforms()
        ids = [a.id for a in addons if a.id not in frozen]
        qs = (UpdateCount.objects.filter(addon__in=ids)
              .values_list('addon').annotate(Avg('count')))
        thisweek = dict(qs.filter(date__gte=one_week))
        threeweek = dict(qs.filter(date__range=(four_weeks, one_week)))
        for addon in addons:
            this, three = thisweek.get(addon.id, 0), threeweek.get(addon.id, 0)
            if this > 1000 and three > 1:
                addon.update(hotness=(this - three) / float(three))
            else:
                addon.update(hotness=0)
        # Let the database catch its breath.
        time.sleep(10)


@cronjobs.register
def recs():
    start = time.time()
    cursor = connections[multidb.get_slave()].cursor()
    cursor.execute("""
        SELECT addon_id, collection_id
        FROM synced_addons_collections ac
        INNER JOIN addons ON
            (ac.addon_id=addons.id AND inactive=0 AND status=4
             AND addontype_id <> 9 AND current_version IS NOT NULL)
        ORDER BY addon_id, collection_id
    """)
    qs = cursor.fetchall()
    recs_log.info('%.2fs (query) : %s rows' % (time.time() - start, len(qs)))
    addons = _group_addons(qs)
    recs_log.info('%.2fs (groupby) : %s addons' %
                  ((time.time() - start), len(addons)))

    # Check our memory usage.
    try:
        p = subprocess.Popen('%s -p%s -o rss' % (settings.PS_BIN, os.getpid()),
                             shell=True, stdout=subprocess.PIPE)
        recs_log.info('%s bytes' % ' '.join(p.communicate()[0].split()))
    except Exception:
        log.error('Could not call ps', exc_info=True)

    sim = recommend.similarity  # Locals are faster.
    sims, start, timers = {}, [time.time()], {'calc': [], 'sql': []}

    def write_recs():
        calc = time.time()
        timers['calc'].append(calc - start[0])
        try:
            _dump_recs(sims)
        except Exception:
            recs_log.error('SQL issue', exc_info=True)
            print idx, 'Error dumping recommendations.'
        sims.clear()
        timers['sql'].append(time.time() - calc)
        start[0] = time.time()

    for idx, (addon, collections) in enumerate(addons.iteritems(), 1):
        xs = [(other, sim(collections, cs))
              for other, cs in addons.iteritems()]
        # Sort by similarity and keep the top N.
        others = sorted(xs, key=operator.itemgetter(1), reverse=True)
        sims[addon] = [(k, v) for k, v in others[:11] if k != addon]

        if idx % 50 == 0:
            write_recs()
    else:
        write_recs()

    avg_len = sum(len(v) for v in addons.itervalues()) / float(len(addons))
    recs_log.info('%s addons: average length: %.2f' % (len(addons), avg_len))
    recs_log.info('Processing time: %.2fs' % sum(timers['calc']))
    recs_log.info('SQL time: %.2fs' % sum(timers['sql']))


def _dump_recs(sims):
    # Dump a dictionary of {addon: (other_addon, score)} into the
    # addon_recommendations table.
    cursor = connections['default'].cursor()
    addons = sims.keys()
    vals = [(addon, other, score) for addon, others in sims.items()
                                  for other, score in others]
    cursor.execute('BEGIN')
    cursor.execute('DELETE FROM addon_recommendations WHERE addon_id IN %s',
                   [addons])
    cursor.executemany("""
        INSERT INTO addon_recommendations (addon_id, other_addon_id, score)
        VALUES (%s, %s, %s)""", vals)
    cursor.execute('COMMIT')


def _group_addons(qs):
    # qs is a list of (addon_id, collection_id) order by addon_id.
    # Return a dict of {addon_id: [collection_id]}.
    addons = {}
    for addon, collections in itertools.groupby(qs, operator.itemgetter(0)):
        # Skip addons in < 3 collections since we'll be overfitting
        # recommendations to exactly what's in those collections.
        cs = [c[1] for c in collections]
        if len(cs) > 3:
            # array.array() lets us calculate similarities much faster.
            addons[addon] = array.array('l', cs)
    # Don't generate recs for frozen add-ons.
    for addon in FrozenAddon.objects.values_list('addon', flat=True):
        if addon in addons:
            recs_log.info('Skipping frozen addon %s.' % addon)
            del addons[addon]
    return addons


@cronjobs.register
@transaction.commit_on_success
def give_personas_versions():
    cursor = connections['default'].cursor()
    path = os.path.join(settings.ROOT, 'migrations/149-personas-versions.sql')
    with open(path) as f:
        cursor.execute(f.read())
        log.info('Gave versions to %s personas.' % cursor.rowcount)


@cronjobs.register
def reindex_addons():
    from . import tasks
    # Make sure our mapping is up to date.
    search.setup_mapping()
    ids = Addon.objects.values_list('id', flat=True)
    with establish_connection() as conn:
        for chunk in chunked(sorted(list(ids)), 150):
            tasks.index_addons.apply_async(args=[chunk], connection=conn)


# TODO(jbalogh): remove after 6.0.12 (bug 659948)
@cronjobs.register
def fix_dupe_appsupport():
    from . import tasks
    # Find all the appsupport (addon, app) rows with duplicate entries.
    qs = (AppSupport.objects.values('addon', 'app')
          .annotate(cnt=Count('id')).filter(cnt__gt=1))
    addons = set(a['addon'] for a in qs)
    # Update appsupport again to fix the dupes.
    tasks.update_appsupport(addons)
