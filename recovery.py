"""Recover desktop project cache from the existing local project database."""
import argparse
import copy
import csv
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize(path):
    return os.path.normcase(os.path.normpath(path.removeprefix('\\\\?\\')))


def find_database(home):
    candidates = []
    for p in home.glob('state_*.sqlite'):
        version = p.stem.removeprefix('state_')
        if version.isdigit():
            candidates.append((int(version), p))
    if not candidates:
        raise RuntimeError('找不到本地项目数据库；不会根据文件夹猜测或创建项目。')
    return max(candidates)[1]


def read_database(path):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('数据库完整性检查失败，停止修复。')
        required = {'projects': {'id', 'name', 'position', 'created_at_ms', 'updated_at_ms'},
                    'project_roots': {'project_id', 'position', 'path'},
                    'threads': {'id', 'cwd', 'project_id'}}
        for table, columns in required.items():
            actual = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            if not columns <= actual:
                raise RuntimeError('数据库结构不兼容，停止修复：' + table)
        projects = [dict(r) for r in conn.execute('SELECT * FROM projects ORDER BY position,id')]
        roots = {}
        for r in conn.execute('SELECT project_id,path FROM project_roots ORDER BY position'):
            roots.setdefault(r['project_id'], []).append(r['path'])
        threads = [dict(r) for r in conn.execute('SELECT id,cwd,project_id FROM threads')]
        return projects, roots, threads
    finally:
        conn.close()


def snapshots(home):
    paths = [p for p in home.iterdir() if p.is_file() and
             (p.name == '.codex-global-state.json.bak' or
              p.name.startswith('..codex-global-state.json.tmp-'))]
    good, bad = [], []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime):
        try:
            data = load(path)
            if not isinstance(data, dict):
                raise ValueError('not an object')
            good.append((path, data))
        except (OSError, ValueError):
            bad.append(path.name)
    return good, bad


def plan(home, restore_threads=False, infer_exact_cwd=False):
    home = Path(home).resolve()
    state_file = home / '.codex-global-state.json'
    current = load(state_file)
    if not isinstance(current, dict):
        raise RuntimeError('当前配置不是 JSON 对象，停止修复。')
    database = find_database(home)
    projects, roots, threads = read_database(database)
    previous, ignored = snapshots(home)
    host = 'local:' + str(home)
    maps = copy.deepcopy(current.get('app-server-project-id-by-legacy-project-id-by-host', {}))
    mapping, historical_assignments, projectless = {}, {}, set(current.get('projectless-thread-ids', []))
    for _, old in previous:
        mapping.update(old.get('app-server-project-id-by-legacy-project-id-by-host', {}).get(host, {}))
        historical_assignments.update(old.get('thread-project-assignments', {}))
        projectless.update(old.get('projectless-thread-ids', []))
    mapping.update(maps.get(host, {}))
    current_cache = current.get('local-projects', {})
    if not isinstance(current_cache, dict):
        raise RuntimeError('local-projects 结构不兼容，停止修复。')
    valid_ids = {p['id'] for p in projects}
    # Preserve the identities used by the current desktop cache first.
    reverse = {server: legacy for legacy, server in mapping.items() if server in valid_ids}
    for legacy in current_cache:
        if legacy in valid_ids:
            reverse[legacy] = legacy
        if legacy in mapping and mapping[legacy] in valid_ids:
            reverse[mapping[legacy]] = legacy
    candidate = copy.deepcopy(current)
    cache = copy.deepcopy(current_cache)
    project_order = list(current.get('project-order', []))
    project_order = list(dict.fromkeys(project_order))
    records, missing, server_to_cache, paths_to_cache = [], [], {}, {}
    for p in projects:
        pid = reverse.get(p['id'], p['id'])
        paths = roots.get(p['id'], [])
        if not paths or not all(isinstance(x, str) and os.path.isabs(x) for x in paths):
            raise RuntimeError('项目根路径缺失或不兼容，停止修复：' + p['id'])
        server_to_cache[p['id']] = pid
        for path in paths:
            paths_to_cache.setdefault(normalize(path), []).append(pid)
        exists = all(Path(path).is_dir() for path in paths)
        record = dict(id=pid, name=p['name'], rootPaths=paths, directoriesPresent=exists)
        records.append(record)
        if pid in cache:
            if [normalize(x) for x in cache[pid]['rootPaths']] != [normalize(x) for x in paths]:
                raise RuntimeError('配置和数据库中的项目路径冲突，停止修复：' + p['name'])
        else:
            missing.append(record)
            cache[pid] = dict(id=pid, name=p['name'], rootPaths=paths,
                              createdAt=p['created_at_ms'], updatedAt=p['updated_at_ms'])
        mapping[pid] = p['id']
        if pid not in project_order:
            project_order.append(pid)
    candidate['local-projects'] = cache
    candidate['project-order'] = project_order
    maps[host] = mapping
    candidate['app-server-project-id-by-legacy-project-id-by-host'] = maps
    restored, inferred = 0, 0
    assignments = copy.deepcopy(current.get('thread-project-assignments', {}))
    if restore_threads:
        available_threads = {t['id'] for t in threads}
        for tid, value in historical_assignments.items():
            if (tid in available_threads and tid not in assignments and tid not in projectless and
                    value.get('projectKind') == 'local' and value.get('projectId') in cache):
                assignments[tid] = value
                restored += 1
        for t in threads:
            if t['id'] in assignments or t['id'] in projectless:
                continue
            pid = server_to_cache.get(t['project_id'])
            if pid is None and infer_exact_cwd:
                matches = paths_to_cache.get(normalize(t['cwd']), [])
                if len(matches) == 1:
                    pid = matches[0]
                    inferred += 1
            if pid:
                assignments[t['id']] = dict(projectKind='local', projectId=pid)
        candidate['thread-project-assignments'] = assignments
        member_hosts = dict(current.get('thread-project-membership-host-ids', {}))
        for tid, value in assignments.items():
            if value.get('projectKind') == 'local':
                member_hosts.setdefault(tid, 'local')
        candidate['thread-project-membership-host-ids'] = member_hosts
        if assignments != current.get('thread-project-assignments', {}):
            migration = copy.deepcopy(current.get('app-server-projects-migration-by-host', {}))
            migration[host] = dict(version=1, projectsMigrated=True,
                threadAssignmentsMigrated=False, threadAssignmentsReadMigrated=False,
                pendingThreadAssignmentIds=[tid for tid,v in assignments.items() if v.get('projectKind') == 'local'])
            candidate['app-server-projects-migration-by-host'] = migration
    if not missing:
        # Do not manufacture configuration changes when the project cache is intact.
        for key in ('local-projects', 'project-order', 'app-server-project-id-by-legacy-project-id-by-host'):
            if key in current:
                candidate[key] = copy.deepcopy(current[key])
            else:
                candidate.pop(key, None)
    for key, value in current_cache.items():
        if candidate['local-projects'][key] != value:
            raise RuntimeError('现有项目被改变，停止修复。')
    changed = [k for k in candidate if candidate.get(k) != current.get(k)]
    report = dict(status='diagnosed', database=str(database), databaseProjects=len(projects),
        cachedProjects=len(current_cache), missingCacheProjects=len(missing),
        missingButDirectoryPresent=sum(r['directoriesPresent'] for r in missing),
        missingDirectories=[r for r in records if not r['directoriesPresent']],
        missingProjects=missing, ignoredInvalidSnapshots=ignored,
        historicalThreadLinksRestored=restored, exactCwdLinksInferred=inferred,
        changedFields=changed, needsRepair=bool(changed),
        uiVerified=False, note='检测的是本地界面配置，不能直接证明当前屏幕的显示状态。')
    return state_file, database, current, candidate, report


def desktop_running():
    if os.name != 'nt':
        raise RuntimeError('此脚本仅支持 Windows，无法验证应用是否退出。')
    # Conservative: also wait for another ChatGPT/Codex desktop instance.
    for image in ('ChatGPT.exe', 'Codex.exe'):
        result = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq ' + image, '/FO', 'CSV', '/NH'],
            capture_output=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError('无法检查应用进程，停止修复。')
        rows = csv.reader(io.StringIO(result.stdout.decode('utf-8', errors='replace')))
        if any(row and row[0].lower() == image.lower() for row in rows):
            return True
    return False


def wait_for_exit(timeout):
    deadline = time.monotonic() + timeout
    print('请完全退出 Codex（包括托盘），脚本会等待。不要关闭本 PowerShell 窗口。', flush=True)
    while time.monotonic() < deadline:
        if not desktop_running():
            time.sleep(3)
            if not desktop_running():
                return
        time.sleep(2)
    raise TimeoutError('等待退出超时，没有修改配置。')


def apply(home, output, restore_threads=False, infer_exact_cwd=False):
    state_file, database, current, candidate, report = plan(home, restore_threads, infer_exact_cwd)
    if not report['needsRepair']:
        report['status'] = 'no_changes_needed'
        return report
    if desktop_running():
        raise RuntimeError('Codex 仍在运行，没有修改配置。')
    source_bytes = state_file.read_bytes()
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    backup = output / ('backup-' + stamp)
    backup.mkdir(parents=True)
    shutil.copy2(state_file, backup / state_file.name)
    companion = state_file.with_name(state_file.name + '.bak')
    if companion.exists():
        shutil.copy2(companion, backup / companion.name)
    src = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(backup / database.name)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    pending = state_file.with_name('.codex-project-recovery-' + uuid.uuid4().hex + '.tmp')
    try:
        save(pending, candidate)
        if load(pending) != candidate:
            raise RuntimeError('候选配置读回不一致。')
        # Rebuild from current DB/state immediately before committing; reject races.
        _, _, _, latest, _ = plan(home, restore_threads, infer_exact_cwd)
        if desktop_running() or state_file.read_bytes() != source_bytes or latest != candidate:
            raise RuntimeError('应用重新启动或数据发生变化，没有写入修复。')
        os.replace(pending, state_file)
        if load(state_file) != candidate:
            shutil.copy2(backup / state_file.name, state_file)
            raise RuntimeError('写入校验失败，已回滚配置。')
    finally:
        if pending.exists():
            pending.unlink()
    report.update(status='applied_awaiting_ui_verification', backup=str(backup),
                  cachedProjectsAfter=len(candidate['local-projects']), readbackVerified=True)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', default=os.environ.get('CODEX_HOME') or str(Path.home() / '.codex'))
    parser.add_argument('--output', default=str(Path(__file__).resolve().parent / 'recovery-results'))
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--restore-thread-links', action='store_true')
    parser.add_argument('--infer-exact-cwd', action='store_true')
    parser.add_argument('--wait-seconds', type=int, default=1800)
    args = parser.parse_args()
    if args.infer_exact_cwd and not args.restore_thread_links:
        parser.error('--infer-exact-cwd 需要 --restore-thread-links')
    home, output = Path(args.home).resolve(), Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _, _, _, _, report = plan(home, args.restore_thread_links, args.infer_exact_cwd)
    save(output / 'report.json', report)
    print(f"数据库项目 {report['databaseProjects']} 个，界面缓存 {report['cachedProjects']} 个，缺失 {report['missingCacheProjects']} 个。", flush=True)
    print(f"缺失项中目录仍存在：{report['missingButDirectoryPresent']} 个；完整报告：{output / 'report.json'}", flush=True)
    for record in report['missingProjects']:
        print('  ' + record['name'] + (' [目录存在]' if record['directoriesPresent'] else ' [旧路径不存在]'), flush=True)
    if args.repair and report['needsRepair']:
        lock = home / '.codex-project-recovery.lock'
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode('ascii'))
        os.close(fd)
        try:
            report['status'] = 'waiting_for_app_exit'
            save(output / 'report.json', report)
            wait_for_exit(args.wait_seconds)
            report = apply(home, output, args.restore_thread_links, args.infer_exact_cwd)
            save(output / 'report.json', report)
            print('配置已修复并读回校验。现在可以重新打开 Codex，检查项目列表及旧聊天。', flush=True)
        finally:
            lock.unlink(missing_ok=True)
    elif not report['needsRepair']:
        print('没有检测到此脚本可修复的配置缺失。如果界面仍为空，需要继续检查加载、筛选或账号状态。', flush=True)
    else:
        print('只读诊断结束，未修改配置。加 -Repair 才会修复。', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('操作失败：' + str(exc), flush=True)
        raise SystemExit(1)
