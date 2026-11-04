"""Read-only, bounded accounting from registered Claude Code session metadata.

Never use normalized transcript text or add result aggregates to message usage.
There is deliberately no ledger, credential lookup, network call or migration.
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
from urllib.parse import quote

TASK_LIMIT = 100
RUN_LIMIT = 500
SESSION_LIMIT = 100
MODEL_LIMIT = 100
PROVIDER_LIMIT = 100
DIRECTORY_LIMIT = 256
FILE_BYTES = 2 * 1024 * 1024
TOTAL_BYTES = 8 * 1024 * 1024
LINE_BYTES = 256 * 1024
ROW_LIMIT = 20000
COUNTERS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
SAFE_INTEGER = 2**53 - 1


def window(query):
    """UTC calendar days; end is exclusive. Reject overflow rather than wrap."""
    try:
        days = int(query.get('days', ['7'])[0])
        if not 1 <= days <= 31:
            raise ValueError()
        raw = query.get('start', [None])[0]
        start = date.fromisoformat(raw) if raw is not None else datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        if raw is not None and raw != start.isoformat():
            raise ValueError()
        end = start + timedelta(days=days)
        return start, end
    except (ValueError, TypeError, OverflowError):
        raise ValueError('invalid usage date range') from None


def plan(cfg, db, workers):
    """Read metadata on an independent read-only connection, never the event loop."""
    def all_rows(sql, *args):
        return db.execute(sql, args).fetchall()

    def get(key, default=None):
        row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default
    session = get('router_session')
    task_limit = TASK_LIMIT - bool(session)
    tasks = all_rows('SELECT id,code_name,session_id,profile,started_once FROM tasks ORDER BY seq DESC LIMIT ?', task_limit + 1)
    truncated = len(tasks) > task_limit
    tasks = tasks[:task_limit]
    ids = [row['id'] for row in tasks]
    runs = all_rows('SELECT task_id,session_id,profile,started,ended FROM worker_runs WHERE task_id IN (' +
                     ','.join('?' for _ in ids) + ') ORDER BY id DESC LIMIT ?', *ids, RUN_LIMIT + 1) if ids else []
    truncated |= len(runs) > RUN_LIMIT
    sources = []
    for row in tasks + runs[:RUN_LIMIT]:
        if 'started_once' in row.keys() and not row['started_once']:
            continue
        task_id = row['id'] if 'id' in row.keys() else row['task_id']
        profile = cfg.profiles.get(row['profile'])
        harness = get('task_harness:' + task_id, profile.harness if profile else '')
        sources.append((task_id, harness, row['session_id'],
                        row['started'] if 'started' in row.keys() else None,
                        row['ended'] if 'ended' in row.keys() else None))
    if session:
        profile = cfg.profiles.get(get('router_profile', cfg.primary))
        sources.append(('router', get('router_harness', profile.harness if profile else ''), session, None, None))
    return {'tasks': ids, 'code_names': {row['id']: row['code_name'] for row in tasks},
            'sources': sources, 'truncated': truncated,
            'workers': dict(workers, queued=db.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]),
            'providers': sorted(cfg.providers)[:PROVIDER_LIMIT],
            'providers_truncated': len(cfg.providers) > PROVIDER_LIMIT}


def read_report(cfg, workers, start, end):
    """One read transaction; no Store constructor or schema migration."""
    db = sqlite3.connect('file:' + quote(str(cfg.db.resolve()), safe='/') + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        snapshot = plan(cfg, db, workers)
    finally:
        db.close()
    return report(cfg, snapshot, start, end)


def quota_snapshot(provider, readers=None):
    """Optional in-process provider hook. No readers are installed by default.

    A future trusted reader may return token limit/remaining, never raw payloads.
    Hooks are not TOML commands or URLs and are never used for admission control.
    """
    reader = (readers or {}).get(provider)
    result = {'provider': provider, 'status': 'unconfigured'}
    if reader is None:
        return result
    try:
        value = reader()
        amounts = {key: value[key] for key in ('limit', 'remaining')}
        if (any(type(n) is not int or not 0 <= n <= SAFE_INTEGER for n in amounts.values()) or
                amounts['remaining'] > amounts['limit']):
            raise ValueError()
        return {'provider': provider, 'status': 'available', 'unit': 'tokens', **amounts}
    except Exception:
        return {'provider': provider, 'status': 'unknown'}


def bucket(**labels):
    return dict(labels, tokens=None, known_tokens=None, unknown_sessions=0, unknown_runs=0,
                **dict.fromkeys(COUNTERS, None))


def add(target, counts):
    for key, value in zip(COUNTERS, counts):
        target[key] = (target[key] or 0) + value
    target['known_tokens'] = sum(target[key] for key in COUNTERS)


def stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SessionFiles:
    """Anchor project/file opens to directory descriptors, not checked pathnames."""
    def __init__(self, root):
        self.fd, self.folders, self.uncertain, self.limited = None, [], False, False
        try:
            self.fd = os.open(root, DIRECTORY_FLAGS)
            with os.scandir(self.fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= DIRECTORY_LIMIT:
                        self.limited = self.uncertain = True
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            self.folders.append(entry.name)
                    except OSError:
                        self.uncertain = True
            self.folders.sort()
        except OSError:
            self.uncertain = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)

    def read(self, session, budget):
        candidates, uncertain = [], self.uncertain
        if self.fd is None:
            return [], True, 0, self.limited
        try:
            for folder in self.folders:
                try:
                    fd = os.open(folder, DIRECTORY_FLAGS, dir_fd=self.fd)
                except OSError:
                    uncertain = True
                    continue
                try:
                    os.stat(session + '.jsonl', dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.close(fd)
                except OSError:
                    uncertain = True
                    os.close(fd)
                else:
                    candidates.append(fd)
            if len(candidates) != 1:
                return [], True, 0, self.limited
            rows, incomplete, used, limited = session_rows(Path(session + '.jsonl'), budget, dir_fd=candidates[0])
            return rows, incomplete or uncertain, used, limited or self.limited
        finally:
            for fd in candidates:
                os.close(fd)


def session_rows(path, budget, *, dir_fd=None):
    """Keep metadata only; fstat the actual nonblocking, no-follow descriptor."""
    records, incomplete, conflicts, limited = {}, False, set(), False
    parent, fd = None, None
    try:
        if dir_fd is None:
            parent = os.open(path.parent, DIRECTORY_FLAGS)
            dir_fd = parent
        fd = os.open(path.name, FILE_FLAGS, dir_fd=dir_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return [], True, 0, False
        with os.fdopen(fd, 'rb') as source:
            fd = None
            data = source.read(min(FILE_BYTES, budget) + 1)
    except OSError:
        return [], True, 0, False
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)
    used = len(data)
    if used > min(FILE_BYTES, budget):
        return [], True, used, True
    for index, line in enumerate(data.split(b'\n')):
        if not line:
            continue
        if index >= ROW_LIMIT:
            incomplete = limited = True
            break
        if len(line) > LINE_BYTES:
            incomplete = limited = True
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            incomplete = True
            continue
        if not isinstance(row, dict):
            incomplete = True
            continue
        if row.get('type') != 'assistant':
            continue
        message = row.get('message')
        if not isinstance(message, dict):
            incomplete = True
            continue
        mid = message.get('id')
        day = stamp(row.get('timestamp'))
        model = message.get('model')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', model):
            model = None
        if not isinstance(mid, str) or not 1 <= len(mid) <= 256 or day is None:
            incomplete = True
            continue
        usage = message.get('usage')
        counts = [usage.get(key) for key in COUNTERS] if isinstance(usage, dict) else []
        if len(counts) != len(COUNTERS) or any(type(n) is not int or not 0 <= n <= SAFE_INTEGER for n in counts):
            counts = None
        previous = records.get(mid)
        if previous:
            # One message can be repeated for separate content blocks/stream snapshots.
            old_day, old_model, old_counts = previous
            day = min(old_day, day)
            if old_model != model or mid in conflicts:
                incomplete = True
                conflicts.add(mid)
                records[mid] = (day, None, None)
                continue
            if old_counts is not None:
                counts = [max(a, b) for a, b in zip(old_counts, counts)] if counts is not None else old_counts
        records[mid] = (day, model, counts)
    if data and not data.endswith(b'\n'):
        incomplete = True
    return list(records.values()), incomplete, used, limited


def report(cfg, snapshot, start, end, quota_readers=None):
    tasks = {tid: bucket(task_id=tid) for tid in snapshot['tasks']}
    days = {start + timedelta(days=i): bucket(day=(start + timedelta(days=i)).isoformat())
            for i in range((end - start).days)}
    models, summary = {}, bucket()
    truncated = snapshot['truncated'] or snapshot['providers_truncated']
    # Session ownership conflicts cannot safely be attributed to either task.
    owners, intervals = {}, {}
    for tid, harness, session, began, ended in snapshot['sources']:
        key = (harness, session)
        owners.setdefault(key, set()).add(tid)
        if began is not None:
            intervals.setdefault(key, set()).add((began, ended))
    budget, samples = TOTAL_BYTES, {}
    with SessionFiles(cfg.session_root) as files:
        truncated |= files.limited
        for index, (key, tids) in enumerate(owners.items()):
            harness, session = key
            rows, incomplete, used, limited = [], True, 0, False
            if (index < SESSION_LIMIT and budget > 0 and harness == 'claude-code' and
                    isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', session) and len(tids) == 1):
                rows, incomplete, used, limited = files.read(session, budget)
            if index >= SESSION_LIMIT or budget <= 0 or limited:
                truncated = True
            budget -= used
            samples[key] = rows, incomplete
    unresolved = 0
    for (harness, session), tids in owners.items():
        rows, incomplete = samples[(harness, session)]
        tid = next(iter(tids))
        target = tasks.setdefault(tid, bucket(task_id=tid))
        in_window = [row for row in rows if start <= row[0].date() < end]
        start_at = datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()
        end_at = datetime.combine(end, datetime.min.time(), timezone.utc).timestamp()
        unknown_runs = sum(1 for began, ended in intervals.get((harness, session), ())
                           if began < end_at and (ended is None or ended > start_at) and
                           not any(counts is not None and began <= when.timestamp() and
                                   (ended is None or when.timestamp() < ended) for when, _, counts in in_window))
        target['unknown_runs'] += unknown_runs
        summary['unknown_runs'] += unknown_runs
        unknown = incomplete or not rows or unknown_runs > 0 or any(row[2] is None for row in in_window)
        if unknown:
            unresolved += 1
            for owner in tids:
                tasks.setdefault(owner, bucket(task_id=owner))['unknown_sessions'] += 1
        unknown_models = set()
        for when, model, counts in in_window:
            group = models.setdefault(model, bucket(model=model))
            if counts is not None:
                for item in (target, days[when.date()], group, summary):
                    add(item, counts)
            elif model not in unknown_models:
                group['unknown_sessions'] += 1
                unknown_models.add(model)
    # Unlocated usage might fall on any day/model. Do not invent its timestamp.
    if unresolved:
        models.setdefault(None, bucket(model=None))
        for model in models.values():
            model['unknown_sessions'] = unresolved
    summary['unknown_sessions'] = unresolved
    for day in days.values():
        day['unknown_sessions'] = unresolved
    # Queue-only/no-record tasks are unknown, not measured zero; no run is invented.
    if snapshot['truncated']:
        summary['unknown_sessions'] += 1
        for item in list(tasks.values()) + list(days.values()):
            item['unknown_sessions'] += 1
    model_rows = sorted(models.values(), key=lambda row: row['model'] or '')
    if len(model_rows) > MODEL_LIMIT:
        truncated = True
        model_rows = model_rows[:MODEL_LIMIT]
    for item in list(tasks.values()) + list(days.values()) + model_rows + [summary]:
        item['tokens'] = item['known_tokens'] if not item['unknown_sessions'] else None
    if truncated:
        summary['tokens'] = None
    for tid, item in tasks.items():
        item['code_name'] = 'Router' if tid == 'router' else snapshot['code_names'].get(tid)
    return {'start': start.isoformat(), 'end': end.isoformat(), 'timezone': 'UTC',
            'workers': snapshot['workers'], 'summary': summary, 'days': list(days.values()),
            'models': model_rows, 'tasks': list(tasks.values()), 'truncated': truncated,
            'quota': [quota_snapshot(provider, quota_readers) for provider in snapshot['providers']],
            'limits': {'tasks': TASK_LIMIT, 'runs': RUN_LIMIT, 'sessions': SESSION_LIMIT, 'models': MODEL_LIMIT,
                       'providers': PROVIDER_LIMIT, 'file_bytes': FILE_BYTES, 'total_bytes': TOTAL_BYTES,
                       'rows_per_file': ROW_LIMIT, 'line_bytes': LINE_BYTES, 'directories': DIRECTORY_LIMIT}}
