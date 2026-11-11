"""Offline transfer of local Codex projects' conversation data (Python 3.14+)."""
import argparse
import hashlib
import json
import os
import queue
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from compression import zstd
from contextlib import closing
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

STATE = "state_5.sqlite"
HISTORY = "thread_history_1.sqlite"
INDEX = "session_index.jsonl"
SIDEBAR = ".codex-global-state.json"
SESSION_DIRS = {"sessions", "archived_sessions"}
SECTIONS = "thread_sections"
SKILL_PREFIX = "user-skills"
STATE_TABLES = ("projects", "project_roots", "threads", "thread_dynamic_tools",
                "thread_spawn_edges")
HISTORY_TABLES = ("thread_turns", "thread_items", "thread_history_projection_state",
                  "thread_realtime_items")
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROLLOUT = re.compile(rf"rollout-.*?-({UUID})(?:_({UUID}))?\.jsonl(?:\.zst)?$", re.I)
CLEANUP_DAYS = 7
BACKUP_NAME = re.compile(r"conversation-transfer-backup-\d{8}-\d{6}-\d{6}$")
STATE_TEMP_NAME = re.compile(
    rf"\.\.codex-global-state\.json(?:\.bak)?\.tmp-\d+-{UUID}$", re.I)
BACKUP_FILES = {STATE, HISTORY, INDEX, SIDEBAR, "new-index.tmp", "new-sidebar.tmp"}


def require_stopped():
    if os.name != "nt":
        raise ValueError("This release supports Windows on both computers.")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-Process | Select-Object -ExpandProperty ProcessName"],
        check=True, capture_output=True, text=True, errors="replace",
    )
    active = {name for name in result.stdout.splitlines()
              if re.fullmatch(r"(?:codex|chatgpt)(?:[-_ ].*)?", name, re.I)}
    if active:
        raise ValueError("Fully quit Codex / ChatGPT / Codex CLI first: " + ", ".join(sorted(active)))


def read_db(path):
    if not path.is_file():
        raise ValueError(f"Missing database: {path}")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def snapshot(source, target):
    with closing(read_db(source)) as db, closing(sqlite3.connect(target)) as copy:
        db.backup(copy)


def header(path):
    opener = zstd.open if path.name.endswith(".zst") else open
    with opener(path, "rb") as stream:
        row = json.loads(stream.readline(8 * 1024 * 1024))
    if row.get("type") != "session_meta":
        raise ValueError("Missing session_meta")
    return row["payload"]


def path_key(path):
    normalized = os.path.normcase(os.path.normpath(path))
    if normalized.startswith("\\\\?\\unc\\"):
        return "\\\\" + normalized[8:]
    return normalized.removeprefix("\\\\?\\")


def below(path, root):
    path = path_key(path)
    root = path_key(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def legacy_project_assignments(home, projects, roots):
    state_path = home / ".codex-global-state.json"
    if not state_path.is_file():
        return {}
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    project_roots = defaultdict(set)
    for root in roots:
        project_roots[root["project_id"]].add(path_key(root["path"]))
    mapped = {}
    for key, entry in state.get("local-projects", {}).items():
        paths = {path_key(path) for path in entry.get("rootPaths", [])}
        matches = [project for project in projects if paths and project_roots[project] == paths]
        if len(matches) == 1:
            mapped[key] = matches[0]
    return {thread: mapped[entry["projectId"]]
            for thread, entry in state.get("thread-project-assignments", {}).items()
            if isinstance(entry, dict) and entry.get("projectKind") == "local"
            and entry.get("projectId") in mapped
            and state.get("thread-project-membership-host-ids", {}).get(thread, "local") == "local"}


def discover(db, home):
    projects = {r["id"]: dict(r) for r in db.execute("SELECT * FROM projects")}
    roots = list(db.execute("SELECT * FROM project_roots"))
    roots = sorted((r for r in roots if r["path"]), key=lambda r: -len(r["path"]))
    projects = {key: row for key, row in projects.items()
                if any(r["project_id"] == key for r in roots)}
    legacy_owners = legacy_project_assignments(home, projects, roots)
    threads = {r["id"]: dict(r) for r in db.execute("SELECT * FROM threads")}
    owners = {}
    for key, row in threads.items():
        owner = row["project_id"]
        if owner not in projects:
            owner = legacy_owners.get(key) or next(
                (r["project_id"] for r in roots if below(row["cwd"], r["path"])), None)
        if owner:
            owners[key] = owner
    edges = defaultdict(set)
    for parent, child in db.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
        if parent in threads and child in threads:
            edges[parent].add(child)
            edges[child].add(parent)
    files = defaultdict(set)
    rejected = {}
    history_bases = set()
    for folder in sorted(SESSION_DIRS):
        for path in (home / folder).rglob("rollout-*.jsonl*"):
            match = ROLLOUT.fullmatch(path.name)
            if not match or not path.is_file():
                continue
            ids = set(filter(None, match.groups()))
            try:
                meta = header(path)
            except (ValueError, OSError, EOFError, KeyError, zstd.ZstdError) as exc:
                rejected[path] = (ids, str(exc))
                continue
            ids.add(meta["id"])
            for key in ids:
                files[key].add(path)
                edges[key].update(ids - {key})
            base = (meta.get("history_base") or {}).get("thread_id")
            parent = meta.get("forked_from_id")
            for dependency in filter(None, (base, parent)):
                edges[meta["id"]].add(dependency)
                if dependency == base:
                    history_bases.add((meta["id"], dependency))
            if meta["id"] not in threads:
                owner = legacy_owners.get(meta["id"]) or next(
                    (r["project_id"] for r in roots if below(meta.get("cwd", ""), r["path"])), None)
                if owner:
                    owners.setdefault(meta["id"], owner)
    wanted = set(owners) | {key for key, row in threads.items() if row.get("thread_section_id")}
    queue = deque(wanted)
    while queue:
        for dependency in edges[queue.popleft()] - wanted:
            wanted.add(dependency)
            queue.append(dependency)
    selected = {path for key in wanted for path in files[key]}
    problems = [f"Missing history base {base} referenced by {child}"
                for child, base in sorted(history_bases) if child in wanted and not files[base]]
    problems += [f"Unreadable {p.name}: {error}" for p, (ids, error) in rejected.items()
                 if ids & wanted]
    for key in sorted(wanted & threads.keys()):
        row = threads[key]
        saved = Path(row["rollout_path"])
        candidates = [p for p in files[key] if p.name.removesuffix(".zst") == saved.name.removesuffix(".zst")]
        if not candidates:
            problems.append(f"Missing primary rollout for {key}: {saved.name}")
        elif len(candidates) > 1:
            problems.append(f"Multiple copies of primary rollout for {key}: {saved.name}")
        else:
            row["rollout_path"] = candidates[0].relative_to(home).as_posix()
            row["project_id"] = owners.get(key)
            for field in ("creator_account_id", "creator_user_id"):
                if field in row:
                    row[field] = None
    return projects, roots, threads, wanted, selected, problems


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def export_tables(db, package, tables, filters, thread_rows=None):
    for table in tables:
        rows = db.execute(f"SELECT * FROM {quote(table)} WHERE {filters[table]}")
        names = [column[0] for column in rows.description]
        package.execute(f"CREATE TABLE {quote(table)} ({','.join(map(quote, names))})")
        values = (tuple(thread_rows[row["id"]][name] for name in names) for row in rows) \
            if table == "threads" else rows
        if table == "projects":
            values = (tuple("{}" if name == "metadata" else row[name] for name in names)
                      for row in rows)
        placeholders = ",".join("?" for _ in names)
        package.executemany(f"INSERT INTO {quote(table)} VALUES ({placeholders})", values)


def add_ids(db, name, ids):
    db.execute(f"CREATE TEMP TABLE {name} (id TEXT PRIMARY KEY)")
    db.executemany(f"INSERT INTO {name} VALUES (?)", ((key,) for key in ids))


def index_lines(path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                yield json.loads(line)


def skill_roots(home):
    return {"codex": home / "skills", "agents": Path.home() / ".agents" / "skills"}


def is_link(path):
    return path.is_symlink() or path.is_junction()


def raise_walk_error(error):
    raise error


def skill_files(folder):
    files = {}
    for directory, dirs, names in folder.walk(on_error=raise_walk_error):
        for name in dirs:
            if is_link(directory / name):
                raise ValueError(f"Skill contains a link: {directory / name}")
        for name in names:
            entry = directory / name
            if is_link(entry) or not entry.is_file():
                raise ValueError(f"Invalid or linked skill file: {entry}")
            files[entry.relative_to(folder)] = entry
    return files


def collect_skills(home):
    skills = {}
    for scope, root in skill_roots(home).items():
        if is_link(root) or is_link(root.parent):
            raise ValueError(f"Skill root is a link: {root}")
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            if not folder.name.startswith(".") and folder.is_dir() and (folder / "SKILL.md").is_file():
                if is_link(folder):
                    raise ValueError(f"Skill directory is a link: {folder}")
                skills[scope, folder.name] = skill_files(folder)
    return skills


def export(home, archive, preview, include_skills=True):
    if not preview:
        require_stopped()
    with closing(read_db(home / STATE)) as state:
        projects, roots, threads, wanted, files, problems = discover(state, home)
        print(f"Detected {len(projects)} projects, {len({path_key(r['path']) for r in roots})} distinct roots,"
              f" {len(wanted & threads.keys())} indexed chats,"
              f" {len(files)} rollout files / segments.")
        for problem in problems[:25]:
            print(problem)
        if problems:
            raise ValueError(f"{len(problems)} history problems. No bundle created.")
        if not files:
            raise ValueError("No local project or section conversations found.")
        skills = collect_skills(home) if include_skills else {}
        if include_skills:
            print(f"{len(skills)} user skills selected.")
        if preview:
            return
        if archive.exists():
            raise ValueError(f"Output already exists: {archive}")
        if archive.is_relative_to(home):
            raise ValueError("Save the archive outside CODEX_HOME.")
        with tempfile.TemporaryDirectory(dir=archive.parent) as work:
            work = Path(work)
            state_filter = {"projects": "id IN (SELECT id FROM picked_projects)",
                            "project_roots": "project_id IN (SELECT id FROM picked_projects)",
                            "threads": "id IN (SELECT id FROM picked)",
                            "thread_dynamic_tools": "thread_id IN (SELECT id FROM picked)",
                            "thread_spawn_edges": "parent_thread_id IN (SELECT id FROM picked) "
                                                  "AND child_thread_id IN (SELECT id FROM picked)"}
            add_ids(state, "picked_projects", projects)
            add_ids(state, "picked", wanted)
            with closing(sqlite3.connect(work / STATE)) as subset:
                export_tables(state, subset, STATE_TABLES, state_filter, threads)
                if state.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (SECTIONS,)).fetchone():
                    export_tables(state, subset, (SECTIONS,), {
                        SECTIONS: "1 ORDER BY rowid",
                    })
                subset.commit()
            with closing(read_db(home / HISTORY)) as history, \
                    closing(sqlite3.connect(work / HISTORY)) as subset:
                add_ids(history, "picked", wanted)
                export_tables(history, subset, HISTORY_TABLES,
                              dict.fromkeys(HISTORY_TABLES, "thread_id IN (SELECT id FROM picked)"))
                subset.commit()
            index = [row for row in index_lines(home / INDEX) if row.get("id") in wanted]
            require_stopped()
            package = work / "bundle.zip"
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as zip_file:
                zip_file.writestr("manifest.json", json.dumps({"format": 1, "projects": len(projects),
                                                              "chats": len(wanted & threads.keys())}))
                zip_file.writestr(INDEX, "".join(json.dumps(row) + "\n" for row in index))
                for name in (STATE, HISTORY):
                    zip_file.write(work / name, name)
                for path in sorted(files):
                    zip_file.write(path, path.relative_to(home).as_posix())
                for (scope, folder), entries in sorted(skills.items()):
                    for relative, path in sorted(entries.items()):
                        zip_file.write(path, f"{SKILL_PREFIX}/{scope}/{folder}/{relative.as_posix()}")
            require_stopped()
            os.rename(package, archive)
        print(f"Saved {archive}")


def unpack(archive, work, include_skills):
    required = {"manifest.json", STATE, HISTORY, INDEX}
    with zipfile.ZipFile(archive) as bundle:
        names = set()
        for entry in bundle.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            if name.casefold() in names or path.as_posix() != name or path.is_absolute() or \
                    ".." in path.parts or any(os.path.isreserved(part) for part in path.parts) or "\\" in name or \
                    entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"Invalid archive entry: {name}")
            names.add(name.casefold())
            is_skill = (len(path.parts) >= 4 and path.parts[0] == SKILL_PREFIX
                        and path.parts[1] in {"codex", "agents"} and not path.parts[2].startswith("."))
            if name not in required and not is_skill and not (
                    len(path.parts) >= 2 and path.parts[0] in SESSION_DIRS and ROLLOUT.fullmatch(path.name)):
                raise ValueError(f"Unexpected archive entry: {name}")
            if is_skill and not include_skills:
                continue
            target = work.joinpath(*path.parts)
            if not target.resolve().is_relative_to(work.resolve()):
                raise ValueError(f"Unsafe archive entry: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    if not required <= names or json.loads((work / "manifest.json").read_text())["format"] != 1:
        raise ValueError("Unsupported bundle.")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def plan_skills(home, work):
    planned = {}
    for scope, root in skill_roots(home).items():
        incoming = work / SKILL_PREFIX / scope
        if not incoming.is_dir():
            continue
        if any(is_link(path) or (path.exists() and not path.is_dir()) for path in (root.parent, root)):
            raise ValueError(f"Unsafe skill destination: {root}")
        for folder in sorted(incoming.iterdir()):
            if not (folder / "SKILL.md").is_file():
                raise ValueError(f"Missing SKILL.md in skill: {folder.name}")
            target = root / folder.name
            entries = skill_files(folder)
            if is_link(target) or (target.exists() and not target.is_dir()):
                print(f"Skill conflict, kept destination: {target}")
            elif target.is_dir():
                existing = skill_files(target)
                if existing.keys() != entries.keys() or any(
                        digest(existing[path]) != digest(entries[path]) for path in entries):
                    print(f"Skill conflict, kept destination: {target}")
            else:
                planned[target] = entries
    return planned


def merge_row(db, schema, table, row):
    columns = ",".join(map(quote, row.keys()))
    placeholders = ",".join("?" for _ in row)
    db.execute(f"INSERT INTO {schema}.{quote(table)} ({columns}) VALUES ({placeholders})",
               tuple(row.values()))


def merge_sections(db, source):
    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SECTIONS,)).fetchone():
        return set()
    sections = list(source.execute(f"SELECT * FROM {SECTIONS} ORDER BY rowid"))
    if not sections:
        return set()
    incoming = set(sections[0].keys())
    target = {column[1] for column in db.execute(f"PRAGMA table_info({SECTIONS})")}
    if incoming != target:
        raise ValueError(f"Incompatible Codex schema for {SECTIONS}. Use matching Codex versions.")
    existing = {key for key, in db.execute(f"SELECT id FROM {SECTIONS}")}
    created = set()
    for section in sections:
        key = section["id"]
        if key not in existing:
            merge_row(db, "main", SECTIONS, dict(section))
            created.add(key)
    return created


def import_rows(db, work, home, provider):
    with closing(read_db(work / STATE)) as source, closing(read_db(work / HISTORY)) as history:
        for input_db, schema, tables in ((source, "main", STATE_TABLES),
                                         (history, "hist", HISTORY_TABLES)):
            for table in tables:
                incoming = {r[1] for r in input_db.execute(f"PRAGMA table_info({quote(table)})")}
                target = {r[1] for r in db.execute(f"PRAGMA {schema}.table_info({quote(table)})")}
                if not incoming or incoming != target:
                    raise ValueError(f"Incompatible Codex schema for {table}. Use matching Codex versions.")
        roots = defaultdict(list)
        for row in source.execute("SELECT * FROM project_roots ORDER BY position"):
            roots[row["project_id"]].append(dict(row))
        target_roots = defaultdict(list)
        for key, path in db.execute("SELECT project_id, path FROM project_roots ORDER BY position"):
            target_roots[key].append(path_key(path))
        project_map = {}
        for item in source.execute("SELECT * FROM projects"):
            row = dict(item)
            key = row["id"]
            paths = [path_key(r["path"]) for r in roots[key]]
            matches = [other for other, other_paths in target_roots.items() if other_paths == paths]
            if (key in target_roots and key not in matches) or len(matches) > 1:
                raise ValueError(f"Conflicting project registration: {row['name']}")
            if matches:
                project_map[key] = matches[0]
                continue
            project_map[key] = key
            row["metadata"] = "{}"
            if "position" in row:
                row["position"] = db.execute("SELECT coalesce(max(position), 0) + 1 FROM projects").fetchone()[0]
            merge_row(db, "main", "projects", row)
            for root in roots[key]:
                merge_row(db, "main", "project_roots", root)
        new_sections = merge_sections(db, source)
        existing = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, rollout_path, project_id FROM threads")}
        imported = set()
        memberships = {}
        for item in source.execute("SELECT * FROM threads"):
            row = dict(item)
            key = row["id"]
            relative = PurePosixPath(row["rollout_path"])
            if relative.parts[0] not in SESSION_DIRS or not (work / relative).is_file():
                raise ValueError(f"Invalid primary rollout for {key}")
            if key in existing:
                old_path, current_project = existing[key]
                old = Path(old_path)
                if not old.is_file() or digest(old) != digest(work / relative):
                    raise ValueError(f"Target has a different history for {key}; no changes applied.")
                project = current_project or project_map.get(row["project_id"])
                if project:
                    memberships[key] = project
                    if not current_project:
                        db.execute("UPDATE threads SET project_id=? WHERE id=?", (project, key))
                if row.get("thread_section_id") in new_sections:
                    db.execute("UPDATE threads SET thread_section_id=?, section_position=?, "
                               "section_entered_at_ms=? WHERE id=? AND thread_section_id IS NULL",
                               (row["thread_section_id"], row["section_position"],
                                row["section_entered_at_ms"], key))
                continue
            row["rollout_path"] = str(home / relative)
            row["project_id"] = project_map.get(row["project_id"])
            if row["project_id"]:
                memberships[key] = row["project_id"]
            if provider:
                row["model_provider"] = provider
            merge_row(db, "main", "threads", row)
            imported.add(key)
        for table in STATE_TABLES[3:]:
            for item in source.execute(f"SELECT * FROM {quote(table)}"):
                row = dict(item)
                key = row.get("thread_id", row.get("child_thread_id"))
                if key in imported:
                    merge_row(db, "main", table, row)
        for table in HISTORY_TABLES:
            primary_key = [column[1] for column in sorted(
                db.execute(f"PRAGMA hist.table_info({quote(table)})"), key=lambda column: column[5])
                if column[5]]
            if not primary_key:
                raise ValueError(f"Missing history primary key: {table}")
            match = " AND ".join(f"{quote(column)}=?" for column in primary_key)
            for item in history.execute(f"SELECT * FROM {quote(table)}"):
                row = dict(item)
                if row["thread_id"] in existing:
                    continue
                # A paginated segment can have projection rows without a threads row.
                # Preserve its existing projection when reimporting the same bundle.
                if db.execute(f"SELECT 1 FROM hist.{quote(table)} WHERE {match}",
                              tuple(row[column] for column in primary_key)).fetchone():
                    continue
                merge_row(db, "hist", table, row)
        return imported, set(existing), set(project_map.values()), memberships


def merge_sidebar(original, db, home, project_ids, memberships):
    state = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    registry = state.setdefault("local-projects", {})
    order = state.setdefault("project-order", [])
    mapping = state.setdefault("app-server-project-id-by-legacy-project-id-by-host", {})
    local_host = f"local:{home}"
    host = next((key for key in mapping if key.startswith("local:")
                 and path_key(key[6:]) == path_key(str(home))), local_host)
    modern_ids = mapping.setdefault(host, {})
    assignments = state.setdefault("thread-project-assignments", {})
    assignment_hosts = state.setdefault("thread-project-membership-host-ids", {})
    legacy_ids = {}
    for project in sorted(project_ids | set(memberships.values())):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        roots = [path for path, in db.execute(
            "SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (project,))]
        if row is None or not roots:
            continue
        root_keys = {path_key(path) for path in roots}
        legacy = next((key for key in registry if modern_ids.get(key) == project), None)
        if legacy is None:
            legacy = next((key for key, entry in registry.items()
                           if not modern_ids.get(key)
                           and {path_key(p) for p in entry.get("rootPaths", [])} == root_keys), None)
        if legacy is None:
            legacy = str(uuid5(NAMESPACE_URL, f"codex-conversation-transfer:{project}"))
            if legacy in registry:
                raise ValueError(f"Conflicting sidebar registration: {project}")
            data = dict(row)
            registry[legacy] = {"id": legacy, "name": data["name"], "rootPaths": roots,
                                "createdAt": data.get("created_at_ms", 0),
                                "updatedAt": data.get("updated_at_ms", 0)}
        modern_ids[legacy] = project
        legacy_ids[project] = legacy
        if legacy not in order:
            order.append(legacy)
    for thread, project in memberships.items():
        if project in legacy_ids and thread not in assignments:
            assignments[thread] = {"projectKind": "local", "projectId": legacy_ids[project]}
            assignment_hosts[thread] = "local"
    return json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def import_bundle(home, archive, preview, provider, include_skills=True):
    require_stopped()
    with tempfile.TemporaryDirectory(prefix="codex-transfer-", dir=archive.parent) as temp:
        work = Path(temp)
        unpack(archive, work, include_skills)
        skills = plan_skills(home, work) if include_skills else {}
        if include_skills:
            print(f"{len(skills)} new user skills.")
        additions = []
        for folder in SESSION_DIRS:
            for path in (work / folder).rglob("*"):
                if not path.is_file():
                    continue
                target = home / path.relative_to(work)
                if not target.resolve().is_relative_to(home):
                    raise ValueError(f"Unsafe destination: {target}")
                if target.exists():
                    if not target.is_file() or digest(path) != digest(target):
                        raise ValueError(f"Different rollout already exists: {target.name}")
                else:
                    additions.append((path, target))
        original_index = (home / INDEX).read_bytes() if (home / INDEX).is_file() else None
        original_sidebar = (home / SIDEBAR).read_bytes() if (home / SIDEBAR).is_file() else None
        # Work on database snapshots, leaving the live profile untouched during preview.
        for name in (STATE, HISTORY):
            snapshot(home / name, work / ("target-" + name))
        with closing(sqlite3.connect(work / ("target-" + STATE))) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("ATTACH DATABASE ? AS hist", (str(work / ("target-" + HISTORY)),))
            db.execute("BEGIN")
            imported, existing, projects, memberships = import_rows(db, work, home, provider)
            merge_sidebar(original_sidebar, db, home, projects, memberships)
            db.commit()
        print(f"{len(imported)} new indexed chats, {len(additions)} new rollout files.")
        print(f"{len(projects)} project registrations, {len(memberships)} chat memberships.")
        with closing(read_db(work / STATE)) as source:
            roots = {Path(path) for path, in source.execute("SELECT path FROM project_roots")}
        missing_roots = []
        for root in sorted(roots):
            if not root.is_absolute():
                raise ValueError(f"Project path must be absolute: {root}")
            if not root.is_dir():
                if root.exists():
                    raise ValueError(f"Project path is occupied by a file: {root}")
                missing_roots.append(root)
        if missing_roots:
            print("Project directories to create:\n" + "\n".join(map(str, missing_roots)))
        if preview:
            return
        require_stopped()
        backup = home / ("conversation-transfer-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.mkdir()
        for name in (STATE, HISTORY):
            snapshot(home / name, backup / name)
        if original_index is not None:
            (backup / INDEX).write_bytes(original_index)
        if original_sidebar is not None:
            (backup / SIDEBAR).write_bytes(original_sidebar)
        previous = set(row.get("id") for row in index_lines(home / INDEX))
        new_rows = [row for row in index_lines(work / INDEX)
                    if row.get("id") not in previous and row.get("id") not in existing]
        created = []
        created_dirs = []
        index_changed = False
        sidebar_changed = False
        index_temp = backup / "new-index.tmp"
        sidebar_temp = backup / "new-sidebar.tmp"
        try:
            for root in missing_roots:
                for directory in (*reversed(root.parents), root):
                    if not directory.is_dir():
                        directory.mkdir()
                        created_dirs.append(directory)
            for target, entries in skills.items():
                for directory in reversed(target.parents):
                    if not directory.is_dir():
                        directory.mkdir()
                        created_dirs.append(directory)
                if any(is_link(path) for path in (target.parent, target.parent.parent)):
                    raise ValueError(f"Unsafe skill destination: {target}")
                target.mkdir()
                created_dirs.append(target)
                for relative, source in entries.items():
                    path = target / relative
                    for directory in reversed(relative.parents):
                        parent = target / directory
                        if not parent.is_dir():
                            parent.mkdir()
                            created_dirs.append(parent)
                    with path.open("xb") as output:
                        created.append(path)
                        with source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, output)
            for source, target in additions:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    created.append(target)
                    with source.open("rb") as incoming:
                        shutil.copyfileobj(incoming, output)
            if new_rows:
                prefix = original_index or b""
                if prefix and not prefix.endswith(b"\n"):
                    prefix += b"\n"
                index_temp.write_bytes(prefix + "".join(json.dumps(r) + "\n" for r in new_rows).encode())
                os.replace(index_temp, home / INDEX)
                index_changed = True
            require_stopped()
            with closing(sqlite3.connect(home / STATE)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("ATTACH DATABASE ? AS hist", (str(home / HISTORY),))
                for schema in ("main", "hist"):
                    if db.execute(f"PRAGMA {schema}.journal_mode=DELETE").fetchone()[0] != "delete":
                        raise ValueError("Could not obtain exclusive access to the target databases.")
                db.execute("BEGIN EXCLUSIVE")
                _, _, projects, memberships = import_rows(db, work, home, provider)
                sidebar = merge_sidebar(original_sidebar, db, home, projects, memberships)
                sidebar_temp.write_bytes(sidebar)
                os.replace(sidebar_temp, home / SIDEBAR)
                sidebar_changed = True
                db.commit()
        except Exception:
            if sidebar_changed:
                if original_sidebar is None:
                    (home / SIDEBAR).unlink()
                else:
                    sidebar_temp.write_bytes(original_sidebar)
                    os.replace(sidebar_temp, home / SIDEBAR)
            for path in reversed(created):
                path.unlink()
            if index_changed:
                if original_index is None:
                    (home / INDEX).unlink()
                else:
                    index_temp.write_bytes(original_index)
                    os.replace(index_temp, home / INDEX)
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        print(f"Imported. Backup: {backup}. Open Codex and check the original chats.")


def cleanup_snapshot(path):
    if is_link(path):
        raise ValueError(f"Cleanup skips links: {path}")
    entries = [path]
    if path.is_dir():
        entries.extend(sorted(path.iterdir()))
        if any(entry.name not in BACKUP_FILES or not entry.is_file() or is_link(entry)
               for entry in entries[1:]):
            raise ValueError(f"Cleanup skips backup with unexpected contents: {path}")
    elif not path.is_file():
        raise ValueError(f"Cleanup skips special file: {path}")
    snapshot = {}
    for entry in entries:
        info = entry.stat()
        snapshot[entry] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
    return snapshot


def cleanup(home, apply=False):
    if not home.is_dir():
        raise ValueError(f"Missing Codex directory: {home}")
    cutoff = time.time_ns() - CLEANUP_DAYS * 24 * 60 * 60 * 1_000_000_000
    children = list(home.iterdir())
    backups = sorted((path for path in children if BACKUP_NAME.fullmatch(path.name)),
                     key=lambda path: path.name, reverse=True)
    protected = backups[0] if backups else None
    candidates = []
    total_bytes = 0
    for path in sorted(children):
        if path == protected:
            continue
        is_backup = bool(BACKUP_NAME.fullmatch(path.name))
        if not is_backup and not STATE_TEMP_NAME.fullmatch(path.name):
            continue
        try:
            if is_backup and not path.is_dir():
                raise ValueError(f"Cleanup skips invalid backup directory: {path}")
            if not is_backup and not path.is_file():
                raise ValueError(f"Cleanup skips invalid temporary file: {path}")
            snapshot = cleanup_snapshot(path)
            if any(info[2] >= cutoff for info in snapshot.values()):
                continue
        except (ValueError, OSError) as error:
            print(error)
            continue
        size = sum(info[1] for entry, info in snapshot.items() if entry.is_file())
        total_bytes += size
        candidates.append((path, snapshot))
        print(f"Cleanup candidate: {path.name} ({size:,} bytes)")
    print(f"{len(candidates)} cleanup candidates, {total_bytes:,} bytes.")
    if protected:
        print(f"Keeping newest import backup: {protected.name}")
    if not apply:
        print("Preview only. Use cleanup --apply to delete these old files.")
        return
    require_stopped()
    for path, snapshot in candidates:
        if not path.resolve().is_relative_to(home.resolve()) or cleanup_snapshot(path) != snapshot:
            raise ValueError(f"Cleanup candidate changed; stopped: {path}")
        if path.is_dir():
            for entry in snapshot:
                if entry != path:
                    entry.unlink()
            path.rmdir()
        else:
            path.unlink()
        print(f"Deleted: {path.name}")


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class TransferWindow:
        def __init__(self, root):
            self.root = root
            self.running = False
            self.cleanup_preview = None
            self.events = queue.Queue()
            self.root.title("Codex 项目会话迁移")
            self.root.geometry("840x720")
            self.root.minsize(680, 640)
            self.root.configure(background="#f5f7fa")
            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.mode = tk.StringVar(value="export")
            self.home = tk.StringVar(value=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            self.archive = tk.StringVar(value=str(Path(__file__).resolve().with_name("codex-chats.zip")))
            self.skills = tk.BooleanVar(value=True)
            self.status = tk.StringVar(value="就绪")
            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure(".", font=("Microsoft YaHei UI", 10))
            style.configure("TFrame", background="#f5f7fa")
            style.configure("TLabel", background="#f5f7fa", foreground="#172b45")
            style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
            style.configure("Hint.TLabel", foreground="#465a70")
            style.configure("TButton", padding=(14, 9))
            style.configure("Primary.TButton", foreground="white", background="#245c9a")
            style.map("Primary.TButton", background=[("disabled", "#d7dfe9"), ("active", "#174679")],
                      foreground=[("disabled", "#536376")])
            style.configure("TRadiobutton", background="#f5f7fa", padding=(0, 9))
            style.configure("TCheckbutton", background="#f5f7fa")
            self.controls = []
            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(0, weight=1)
            body = ttk.Frame(root, padding=28)
            body.grid(sticky="nsew")
            body.columnconfigure(0, weight=1)
            body.rowconfigure(9, weight=1)
            ttk.Label(body, text="Codex 项目会话迁移", style="Title.TLabel").grid(sticky="w")
            ttk.Label(body, text="迁移聊天、分页历史、项目分组和 Skills", style="Hint.TLabel").grid(
                sticky="w", pady=(6, 18))
            modes = ttk.Frame(body)
            modes.grid(row=2, sticky="ew", pady=(0, 14))
            for mode, label in (("export", "导出会话"), ("import", "导入会话"), ("cleanup", "清理旧文件")):
                button = ttk.Radiobutton(modes, text=label, value=mode, variable=self.mode)
                button.pack(side="left", padx=(0, 28))
                self.controls.append(button)
            self.path_field(body, 3, "Codex 数据目录", self.home, self.choose_home)
            self.archive_entry, self.archive_button = self.path_field(
                body, 4, "会话 ZIP", self.archive, self.choose_archive)
            options = ttk.Frame(body)
            options.grid(row=5, sticky="ew", pady=(4, 12))
            self.skills_button = ttk.Checkbutton(options, text="同时迁移用户 Skills", variable=self.skills)
            self.skills_button.pack(side="left")
            self.controls.append(self.skills_button)
            self.hint = ttk.Label(body, style="Hint.TLabel", wraplength=740)
            self.hint.grid(row=6, sticky="ew", pady=(0, 14))
            actions = ttk.Frame(body)
            actions.grid(row=7, sticky="ew", pady=(0, 18))
            self.preview_button = ttk.Button(actions, text="预览", command=lambda: self.start(True))
            self.preview_button.pack(side="left")
            self.run_button = ttk.Button(actions, text="开始导出", style="Primary.TButton",
                                         command=lambda: self.start(False))
            self.run_button.pack(side="left", padx=12)
            self.controls.extend((self.preview_button, self.run_button))
            ttk.Label(body, text="运行记录").grid(row=8, sticky="w", pady=(0, 8))
            log_frame = ttk.Frame(body)
            log_frame.grid(row=9, sticky="nsew")
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            self.log = tk.Text(log_frame, wrap="word", state="disabled", background="white",
                               foreground="#23374d", font=("Microsoft YaHei UI", 10),
                               relief="solid", borderwidth=1, padx=12, pady=10)
            self.log.grid(sticky="nsew")
            scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.log.configure(yscrollcommand=scrollbar.set)
            footer = ttk.Frame(body)
            footer.grid(row=10, sticky="ew", pady=(14, 0))
            footer.columnconfigure(0, weight=1)
            ttk.Label(footer, textvariable=self.status).grid(sticky="w")
            self.progress = ttk.Progressbar(footer, mode="indeterminate", length=140)
            self.progress.grid(row=0, column=1, sticky="e")
            for variable in (self.mode, self.home, self.archive, self.skills):
                variable.trace_add("write", self.selection_changed)
            root.bind("<Configure>", lambda event: self.hint.configure(
                wraplength=max(400, body.winfo_width() - 56)) if event.widget == root else None)
            self.selection_changed()
            self.write_log("选择操作，核对路径后先预览。正式迁移或清理前，请完全退出 Codex。\n")
            self.root.after(100, self.poll)

        def path_field(self, body, row, label, variable, command):
            frame = ttk.Frame(body)
            frame.grid(row=row, sticky="ew", pady=(0, 14))
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=label).grid(row=0, column=0, sticky="w", pady=(0, 6))
            entry = ttk.Entry(frame, textvariable=variable)
            entry.grid(row=1, column=0, sticky="ew")
            button = ttk.Button(frame, text="选择…", command=command)
            button.grid(row=1, column=1, padx=(10, 0))
            self.controls.extend((entry, button))
            return entry, button

        def choose_home(self):
            value = filedialog.askdirectory(parent=self.root, title="选择 Codex 数据目录",
                                             initialdir=self.home.get())
            if value:
                self.home.set(value)

        def choose_archive(self):
            options = dict(parent=self.root, filetypes=[("ZIP 文件", "*.zip")],
                           initialdir=str(Path(self.archive.get()).parent))
            if self.mode.get() == "export":
                value = filedialog.asksaveasfilename(
                    title="导出到 ZIP", defaultextension=".zip", confirmoverwrite=False,
                    initialfile=Path(self.archive.get()).name, **options)
            else:
                value = filedialog.askopenfilename(title="选择会话 ZIP", **options)
            if value:
                self.archive.set(value)

        def selection_changed(self, *_):
            self.cleanup_preview = None
            self.refresh_controls()

        def refresh_controls(self):
            for widget in self.controls:
                widget.configure(state="disabled" if self.running else "normal")
            mode = self.mode.get()
            cleanup_mode = mode == "cleanup"
            if cleanup_mode:
                for widget in (self.archive_entry, self.archive_button, self.skills_button):
                    widget.configure(state="disabled")
            labels = {"export": "开始导出", "import": "开始导入", "cleanup": "清理预览中的文件"}
            self.run_button.configure(text=labels[mode])
            if cleanup_mode and self.cleanup_preview != self.home.get():
                self.run_button.configure(state="disabled")
            hints = {
                "export": "自动识别项目和 Section，保存会话及完整分页历史。导出路径应选择一个新 ZIP。",
                "import": "读取 ZIP，恢复项目分组和聊天。缺失的项目目录会按原路径创建，重复导入会保留已有记录。",
                "cleanup": "只清理超过 7 天的导入备份和全局状态写入临时文件，保留最新一份导入备份。请先预览。",
            }
            self.hint.configure(text=hints[mode])

        def write_log(self, text):
            self.log.configure(state="normal")
            self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")

        def start(self, preview):
            if self.running:
                return
            mode = self.mode.get()
            if not self.home.get().strip() or (mode != "cleanup" and not self.archive.get().strip()):
                messagebox.showerror("路径不完整", "请选择 Codex 数据目录和会话 ZIP。", parent=self.root)
                return
            apply = mode == "cleanup" and not preview
            if apply:
                if self.cleanup_preview != self.home.get():
                    return
                if not messagebox.askyesno("确认清理", "将重新检查并永久删除符合条件的旧文件。\n"
                                          "删除的旧备份将失去对应的回退能力。确认继续？", parent=self.root):
                    return
            home = self.home.get()
            command = [sys.executable, "-u", str(Path(__file__).resolve()), mode,
                       "--codex-home", home]
            if mode != "cleanup":
                command.insert(4, self.archive.get())
                if not self.skills.get():
                    command.append("--exclude-skills")
                if preview:
                    command.append("--preview")
            elif apply:
                command.append("--apply")
            self.running = True
            self.cleanup_preview = None
            self.status.set("正在预览…" if preview else "正在处理…")
            self.write_log("\n" + ("预览" if preview else "执行") +
                           {"export": "导出", "import": "导入", "cleanup": "清理"}[mode] + "\n")
            self.refresh_controls()
            self.progress.start(12)
            threading.Thread(target=self.worker, args=(command, mode, preview, home), daemon=True).start()

        def worker(self, command, mode, preview, home):
            try:
                environment = os.environ.copy()
                environment["PYTHONIOENCODING"] = "utf-8"
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace", env=environment,
                                      creationflags=subprocess.CREATE_NO_WINDOW) as process:
                    for line in process.stdout:
                        self.events.put(("log", line))
                    code = process.wait()
                self.events.put(("done", (code, mode, preview, home)))
            except Exception as error:
                self.events.put(("log", str(error) + "\n"))
                self.events.put(("done", (1, mode, preview, home)))

        def poll(self):
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == "log":
                        self.write_log(value)
                    else:
                        code, mode, preview, home = value
                        self.running = False
                        self.progress.stop()
                        if code == 0:
                            self.status.set("预览完成" if preview else "操作完成")
                            if mode == "cleanup" and preview:
                                self.cleanup_preview = home
                        else:
                            self.status.set("操作未完成，请查看运行记录")
                        self.refresh_controls()
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        def close(self):
            if self.running:
                messagebox.showinfo("任务进行中", "请等待任务结束后再关闭窗口。", parent=self.root)
            else:
                self.root.destroy()

    root = tk.Tk()
    TransferWindow(root)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "import", "cleanup", "gui"))
    parser.add_argument("archive", nargs="?", type=Path,
                        default=Path(__file__).resolve().with_name("codex-chats.zip"),
                        help="ZIP path (default: codex-chats.zip beside this script)")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get(
        "CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="Delete the old files listed by cleanup (default: preview only)")
    skill_options = parser.add_mutually_exclusive_group()
    skill_options.add_argument("--include-skills", dest="include_skills", action="store_true",
                               help="Transfer user skills (default)")
    skill_options.add_argument("--exclude-skills", dest="include_skills", action="store_false",
                               help="Skip user skills during export or import")
    parser.set_defaults(include_skills=True)
    parser.add_argument("--provider", help="Optional target model_provider for imported thread rows")
    if len(sys.argv) == 1:
        launch_gui()
        return
    args = parser.parse_args()
    if args.apply and args.mode != "cleanup":
        parser.error("--apply is only valid with cleanup")
    if args.apply and args.preview:
        parser.error("--apply and --preview cannot be combined")
    if args.mode == "gui":
        launch_gui()
        return
    home, archive = args.codex_home.resolve(), args.archive.resolve()
    if args.mode == "export":
        export(home, archive, args.preview, args.include_skills)
    elif args.mode == "import":
        import_bundle(home, archive, args.preview, args.provider, args.include_skills)
    else:
        cleanup(home, args.apply)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        sys.exit(f"Transfer stopped: {exc}")
