#!/usr/bin/env python3
"""Изолированное dev/test окружение. Не устанавливает системные пакеты и службы.

Все процессы serve и задания jobs работают в одном network namespace. Это также
позволяет запускать настоящие SQL/HTTP тесты в ограниченном executor.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
import uuid


REPO = Path(__file__).resolve().parents[2]
LOCK = json.loads((REPO / "infra/versions.json").read_text())
SITE_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,100}\Z")


def write_json(path: Path, value, *, private=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x") as stream:
        if private:
            temporary.chmod(0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    temporary.replace(path)


def run(argv, *, cwd=None, env=None, **kwargs):
    return subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env, check=True, **kwargs)


def environment(root: Path):
    env = dict(os.environ)
    paths = [
        root / "bench/env/bin", root / "tools/bin", root / "node-tools/node_modules/.bin",
        root / "vendor/mariadb/bin", root / "vendor/redis/src",
        root / "vendor/postgresql/usr/lib/postgresql/17/bin",
    ]
    env["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get("PATH", "")
    libraries = [root / "vendor/postgresql/usr/lib/x86_64-linux-gnu",
                 root / "vendor/debian/usr/lib/x86_64-linux-gnu"]
    env["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, libraries))
    env.update(CC="gcc", CXX="g++", UV_LINK_MODE="copy", FRAPPE_BENCH_ROOT=str(root / "bench"))
    return env


def initialize_directories(root):
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for directory in ("downloads", "vendor", "sources", "config", "data", "logs", "run", "jobs"):
        (root / directory).mkdir(exist_ok=True)
        (root / directory).chmod(0o700)


def download(root, spec):
    target = root / "downloads" / spec["url"].rsplit("/", 1)[-1]
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".partial")
        with urllib.request.urlopen(spec["url"], timeout=90) as source, partial.open("wb") as dest:
            shutil.copyfileobj(source, dest, 1024 * 1024)
        partial.replace(target)
    digest = hashlib.file_digest(target.open("rb"), "sha256").hexdigest()
    if digest != spec["sha256"]:
        raise RuntimeError(f"SHA-256 не совпал: {target.name}; файл не используется")
    return target


def source_checkout(root, name, *, existing=None):
    spec = LOCK[name]
    target = existing or root / "sources" / name
    if not target.exists():
        run(["git", "clone", "--depth", "1", "--branch", spec["tag"], spec["url"], target])
    commit = run(["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True).stdout.strip()
    if commit != spec["commit"]:
        raise RuntimeError(f"{name}: ожидается {spec['commit']}, получен {commit}; checkout не изменён")
    return target.resolve()


def runtime_config(root):
    return json.loads((root / "config/runtime.json").read_text())


def bootstrap(root, *, python, postgres=True):
    initialize_directories(root)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("Этот rootless профиль проверен только на Linux x86_64")
    version = run([python, "-c", "import platform;print(platform.python_version())"],
                  capture_output=True, text=True).stdout.strip()
    if version != LOCK["python"]:
        raise RuntimeError(f"Нужен Python {LOCK['python']}, установлен {version}")
    node = run(["node", "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    if node != LOCK["node"]:
        raise RuntimeError(f"Нужен Node {LOCK['node']}, установлен {node}")
    for program in ("uv", "git", "gcc", "g++", "make", "dpkg-deb", "tar", "gzip", "file"):
        if not shutil.which(program):
            raise RuntimeError(f"Предварительно установите {program}; bootstrap не использует sudo")
    db_archive = download(root, LOCK["mariadb"])
    db_directory = root / "vendor" / LOCK["mariadb"]["directory"]
    if not db_directory.exists():
        with tarfile.open(db_archive) as archive:
            archive.extractall(root / "vendor", filter="data")
    db_link = root / "vendor/mariadb"
    if db_link.exists() and db_link.resolve() != db_directory.resolve():
        raise RuntimeError("MariaDB pin изменён. Создайте новый runtime и восстановите копию; старая база не изменена")
    if not db_link.exists():
        db_link.symlink_to(db_directory.name, target_is_directory=True)
    run(["dpkg-deb", "-x", download(root, LOCK["libaio"]), root / "vendor/debian"])
    redis = source_checkout(root, "redis", existing=root / "vendor/redis")
    run(["make", "-j2", "MALLOC=libc", "BUILD_TLS=no"], cwd=redis)
    if postgres:
        for package in LOCK["postgresql"]["packages"]:
            run(["dpkg-deb", "-x", download(root, package), root / "vendor/postgresql"])
    if not (root / "node-tools/node_modules/.bin/yarn").exists():
        (root / "node-tools").mkdir(exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(REPO / "infra/node-tools" / name, root / "node-tools" / name)
        run(["npm", "ci", "--prefix", root / "node-tools", "--ignore-scripts", "--no-audit",
             "--no-fund", "--registry", "https://registry.npmjs.org"])
    env = environment(root)
    for name, target in (("tools", root / "tools"), ("runtime", root / "bench/env")):
        if not (target / "bin/python").exists():
            run(["uv", "venv", target, "--python", python], env=env)
        run(["uv", "pip", "install", "--python", target / "bin/python", "-r",
             REPO / f"infra/python-{name}.lock"], env=env)
    bench = root / "bench"
    (bench / "apps").mkdir(exist_ok=True)
    for name in ("frappe", "erpnext"):
        link = bench / "apps" / name
        source = source_checkout(root, name, existing=link.resolve() if link.exists() else None)
        if not link.exists():
            link.symlink_to(source, target_is_directory=True)
    link = bench / "apps/erp_basic"
    if link.exists() and link.resolve() != REPO:
        raise RuntimeError("apps/erp_basic указывает на другой проект; каталог не изменён")
    if not link.exists():
        link.symlink_to(REPO, target_is_directory=True)
    setup = """from bench.bench import Bench
import sys
b=Bench(sys.argv[1]);b.setup.dirs();b.setup.logging();b.setup.patches()
"""
    run([root / "tools/bin/python", "-c", setup, bench], env=env)
    (bench / "sites/apps.txt").write_text("frappe\nerpnext\nerp_basic\n")
    run(["uv", "pip", "install", "--python", bench / "env/bin/python", "--no-deps",
         "-e", bench / "apps/frappe", "-e", bench / "apps/erpnext", "-e", link], env=env)
    for name in ("frappe", "erpnext"):
        run(["yarn", "install", "--frozen-lockfile", "--non-interactive", "--registry",
             "https://registry.npmjs.org"], cwd=bench / "apps" / name, env=env)
    config = {"ports": LOCK["ports"], "postgresql": postgres}
    config_path = root / "config/runtime.json"
    if not config_path.exists():
        write_json(config_path, config)
    else:
        config = runtime_config(root)
    ports = config["ports"]
    common = bench / "sites/common_site_config.json"
    values = json.loads(common.read_text()) if common.exists() else {}
    values.update(db_host="127.0.0.1", db_port=ports["mariadb"],
                  redis_cache=f"redis://127.0.0.1:{ports['redis_cache']}",
                  redis_queue=f"redis://127.0.0.1:{ports['redis_queue']}",
                  redis_socketio=f"redis://127.0.0.1:{ports['redis_queue']}",
                  webserver_port=ports["http"], socketio_port=ports["socketio"],
                  gunicorn_workers=2, background_workers=1, serve_default_site=True,
                  developer_mode=1, enable_telemetry=0, restart_supervisor_on_update=False,
                  restart_systemd_on_update=False)
    write_json(common, values)
    credentials = root / "credentials.json"
    if not credentials.exists():
        write_json(credentials, {"db_root_password": secrets.token_hex(24)})
    credentials.chmod(0o600)
    print(f"Зависимости готовы. Секреты: {credentials} (0600); значения не выводятся.")


class Runtime:
    def __init__(self, root):
        self.root = root
        self.env = environment(root)
        self.config = runtime_config(root)
        self.ports = self.config["ports"]
        self.processes = []
        self.initialization_files = []

    def spawn(self, name, argv, *, cwd=None):
        logfile = self.root / "logs" / f"{name}.log"
        stream = logfile.open("a")
        logfile.chmod(0o600)
        process = subprocess.Popen(list(map(str, argv)), cwd=cwd, env=self.env,
                                   stdout=stream, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        stream.close()
        self.processes.append((name, process))
        return process

    def wait_for(self, port, *, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"Сервис на порту {port} не стал готов; проверьте logs")

    def start_databases(self):
        r = self.root
        for name in ("mariadb", "redis_cache", "redis_queue") + (("postgresql",) if self.config["postgresql"] else ()):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", self.ports[name]))
                except OSError as exc:
                    raise RuntimeError(f"Порт {self.ports[name]} занят; чужой процесс не изменён") from exc
        db = r / "vendor/mariadb"
        data = r / "data/mariadb"
        data.mkdir(exist_ok=True)
        data.chmod(0o700)
        credentials = json.loads((r / "credentials.json").read_text())
        if not (data / "mysql").exists():
            with (r / "logs/mariadb-initialize.log").open("w") as output:
                run([db / "scripts/mariadb-install-db", "--no-defaults", f"--basedir={db}",
                     f"--datadir={data}", "--auth-root-authentication-method=normal", "--skip-test-db"],
                    env=self.env, stdout=output, stderr=subprocess.STDOUT)
        config = r / "config/mariadb.cnf"
        config.write_text(f"""[mariadbd]
basedir={db}
datadir={data}
socket=
port={self.ports['mariadb']}
bind-address=127.0.0.1
pid-file={r}/run/mariadb.pid
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
skip-character-set-client-handshake
transaction-isolation=READ-COMMITTED
innodb-buffer-pool-size=256M
max-connections=100
""")
        init = r / "run/mariadb-initialize.sql"
        password = credentials["db_root_password"]
        # Пароль создан secrets.token_hex; никогда не поступает из SQL/HTTP ввода.
        if not re.fullmatch(r"[a-f0-9]{48}", password):
            raise ValueError("Некорректный формат автоматически созданного DB root secret")
        init.write_text(
            "DELETE FROM mysql.global_priv WHERE User='';\n"
            f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{password}';\n"
            f"CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
            "GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION;\n"
            "FLUSH PRIVILEGES;\n"
        )
        init.chmod(0o600)
        self.initialization_files.append(init)
        self.spawn("mariadb", [db / "bin/mariadbd", f"--defaults-file={config}", f"--init-file={init}"])
        self.wait_for(self.ports["mariadb"])
        import pymysql
        with pymysql.connect(host="127.0.0.1", port=self.ports["mariadb"], user="root", password=password) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT @@version, @@tx_isolation")
                version, isolation = cursor.fetchone()
                if not version.startswith(LOCK["mariadb"]["version"]) or isolation != "READ-COMMITTED":
                    raise RuntimeError("Не совпадают версия MariaDB или уровень изоляции")
        init.unlink()
        for name in ("cache", "queue"):
            path = r / "data" / f"redis-{name}"
            path.mkdir(exist_ok=True)
            config = r / "config" / f"redis-{name}.conf"
            config.write_text(f"""bind 127.0.0.1
protected-mode yes
port {self.ports['redis_' + name]}
dir {path}
daemonize no
save ""
appendonly {'yes' if name == 'queue' else 'no'}
maxmemory 256mb
maxmemory-policy {'noeviction' if name == 'queue' else 'allkeys-lru'}
""")
            self.spawn(f"redis-{name}", [r / "vendor/redis/src/redis-server", config])
            self.wait_for(self.ports["redis_" + name])
        if self.config["postgresql"]:
            self.start_postgresql()

    def start_postgresql(self):
        r = self.root
        binaries = r / "vendor/postgresql/usr/lib/postgresql/17/bin"
        data = r / "data/postgresql"
        data.mkdir(exist_ok=True)
        data.chmod(0o700)
        secret_file = r / "postgresql-credentials.json"
        if not secret_file.exists():
            write_json(secret_file, {"host": "127.0.0.1", "port": self.ports["postgresql"],
                                    "dbname": "clientora", "user": "clientora",
                                    "password": secrets.token_urlsafe(32), "admin_user": "erp_runtime_admin",
                                    "admin_password": secrets.token_urlsafe(32)})
        secrets_value = json.loads(secret_file.read_text())
        if not (data / "PG_VERSION").exists():
            secret = r / "run/postgresql-initialize-password"
            secret.write_text(secrets_value["admin_password"])
            secret.chmod(0o600)
            self.initialization_files.append(secret)
            with (r / "logs/postgresql-initialize.log").open("w") as output:
                run([binaries / "initdb", "-D", data, "-U", secrets_value["admin_user"],
                     "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--encoding=UTF8",
                     "--locale=C.UTF-8", f"--pwfile={secret}"], env=self.env,
                    stdout=output, stderr=subprocess.STDOUT)
            secret.unlink()
        # Нельзя удалять postmaster.pid автоматически: data directory может использовать другой процесс.
        self.spawn("postgresql", [binaries / "postgres", "-D", data, "-h", "127.0.0.1",
                                  "-p", self.ports["postgresql"], "-k", "", "-c", "jit=off",
                                  "-c", "shared_buffers=128MB", "-c", "max_connections=50"])
        self.wait_for(self.ports["postgresql"])
        import psycopg2
        from psycopg2 import sql
        deadline = time.monotonic() + 60
        while True:
            self.check()
            try:
                connection = psycopg2.connect(host="127.0.0.1", port=self.ports["postgresql"], dbname="postgres",
                                              user=secrets_value["admin_user"], password=secrets_value["admin_password"],
                                              connect_timeout=5)
                break
            except psycopg2.OperationalError:
                # TCP начинает принимать соединения до завершения WAL recovery.
                if time.monotonic() >= deadline:
                    raise RuntimeError("PostgreSQL не готов к SQL после 60 секунд; см. logs/postgresql.log") from None
                time.sleep(0.2)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (secrets_value["user"],))
            if not cursor.fetchone():
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(secrets_value["user"])),
                               (secrets_value["password"],))
            cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (secrets_value["dbname"],))
            if not cursor.fetchone():
                cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(secrets_value["dbname"]),
                                                                            sql.Identifier(secrets_value["user"])))
        connection.close()

    def start_application(self, *, workers=True):
        bench = self.root / "bench"
        self.spawn("http", [bench / "env/bin/gunicorn", "frappe.app:application_with_statics()", "--bind",
                            f"127.0.0.1:{self.ports['http']}", "--workers", "2", "--threads", "4",
                            "--timeout", "120", "--access-logfile", "-"], cwd=bench / "sites")
        self.wait_for(self.ports["http"])
        if workers:
            self.spawn("worker", [self.root / "tools/bin/bench", "worker", "--queue", "short,default,long"], cwd=bench)
            self.spawn("scheduler", [self.root / "tools/bin/bench", "schedule"], cwd=bench)

    def check(self):
        for name, process in self.processes:
            if process.poll() is not None:
                raise RuntimeError(f"Процесс {name} завершился ({process.returncode}); см. logs/{name}.log")

    def health(self):
        import pymysql
        import redis
        credentials = json.loads((self.root / "credentials.json").read_text())
        with pymysql.connect(host="127.0.0.1", port=self.ports["mariadb"], user="root",
                             password=credentials["db_root_password"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT @@version, @@tx_isolation")
                version, isolation = cursor.fetchone()
        report = {"mariadb": version, "transaction_isolation": isolation,
                  "python": platform.python_version()}
        for name in ("redis_cache", "redis_queue"):
            client = redis.Redis(host="127.0.0.1", port=self.ports[name], socket_timeout=5)
            try:
                if not client.ping():
                    raise RuntimeError(f"{name} не ответил PONG")
                report[name] = client.info("server")["redis_version"]
            finally:
                client.close()
        if self.config["postgresql"]:
            import psycopg2
            secret = json.loads((self.root / "postgresql-credentials.json").read_text())
            with psycopg2.connect(host="127.0.0.1", port=self.ports["postgresql"],
                                  dbname=secret["dbname"], user=secret["user"], password=secret["password"]) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SHOW server_version")
                    report["postgresql"] = cursor.fetchone()[0]
            connection.close()
        return report

    def stop(self):
        for _, process in reversed(self.processes):
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for path in self.initialization_files:
            path.unlink(missing_ok=True)


def create_site(root, site, *, testing=False, basic=True):
    if not SITE_PATTERN.fullmatch(site) or ".." in site:
        raise ValueError("Некорректное имя Site")
    sites = root / "bench/sites"
    if (sites / site).exists():
        raise RuntimeError("Site уже существует; автоматическое стирание/переустановка запрещены")
    os.chdir(sites)
    import frappe
    from frappe.installer import _new_site
    credentials = json.loads((root / "credentials.json").read_text())
    site_credentials = root / "site-credentials" / f"{site}.json"
    site_credentials.parent.mkdir(exist_ok=True)
    site_credentials.parent.chmod(0o700)
    if site_credentials.exists():
        raise RuntimeError("Секреты для Site уже существуют; проверьте предыдущую установку")
    admin_password = secrets.token_urlsafe(32)
    write_json(site_credentials, {"site": site, "user": "Administrator", "password": admin_password})
    config = runtime_config(root)
    frappe.init(site=site, new_site=True)
    _new_site(None, site, db_root_username="root", db_root_password=credentials["db_root_password"],
              admin_password=admin_password, db_type="mariadb", db_host="127.0.0.1",
              db_port=config["ports"]["mariadb"], mariadb_user_host_login_scope="127.0.0.1",
              install_apps=["erpnext", "erp_basic"] if basic else ["erpnext"])
    path = sites / site / "site_config.json"
    values = json.loads(path.read_text())
    values.update(allow_tests=bool(testing), enable_telemetry=0, mute_emails=1,
                  erp_basic_disable_external_calls=1, pause_scheduler=1)
    write_json(path, values)
    common = sites / "common_site_config.json"
    values = json.loads(common.read_text())
    values.setdefault("default_site", site)
    write_json(common, values)
    frappe.destroy()
    print(f"Site {site} установлен; исходящие отправки и scheduler выключены. Отдельный пароль: {site_credentials} (0600).")


def perform_job(root, request, env):
    token = request.name.removesuffix(".request.json")
    running = request.with_name(token + ".running.json")
    request.rename(running)
    running.chmod(0o600)
    started = time.monotonic()
    output, error = (request.parent / (token + suffix) for suffix in (".stdout.log", ".stderr.log"))
    returncode, timed_out = 1, False
    try:
        spec = json.loads(running.read_text())
        argv = spec["argv"]
        extra = spec.get("env", {})
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("argv должен быть непустым массивом строк")
        if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
            raise ValueError("env должен содержать только строки")
        timeout = float(spec.get("timeout", 600))
        if not 0 < timeout <= 3600:
            raise ValueError("timeout должен быть от 0 до 3600 секунд")
        with output.open("w") as stdout, error.open("w") as stderr:
            output.chmod(0o600)
            error.chmod(0o600)
            process = subprocess.Popen(argv, cwd=spec.get("cwd", str(root / "bench")),
                                       env={**env, **extra}, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                returncode = 124
    except Exception:
        error.write_text(traceback.format_exc())
        error.chmod(0o600)
    result = {"returncode": returncode, "timed_out": timed_out,
              "duration_seconds": round(time.monotonic() - started, 3),
              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "stdout": str(output), "stderr": str(error)}
    write_json(request.parent / (token + ".result.json"), result)
    print(f"Job {token}: code={returncode}, duration={result['duration_seconds']}s", flush=True)


def serve(root, *, application=True, workers=True, queue=False):
    runtime = Runtime(root)
    ready = root / "ready.json"
    def stop_signal(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop_signal)
    try:
        runtime.start_databases()
        if application:
            runtime.start_application(workers=workers)
        write_json(ready, {"pid": os.getpid(), "ports": runtime.ports, "job_queue": bool(queue)})
        print("ERP Basic runtime готов; все адреса привязаны к loopback.", flush=True)
        while True:
            runtime.check()
            if queue:
                for request in sorted((root / "jobs").glob("*.request.json")):
                    perform_job(root, request, runtime.env)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        ready.unlink(missing_ok=True)
        runtime.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO.parent / "erp-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("bootstrap")
    setup.add_argument("--python", default=sys.executable)
    setup.add_argument("--without-postgresql", action="store_true")
    server = commands.add_parser("serve")
    server.add_argument("--services-only", action="store_true")
    server.add_argument("--without-workers", action="store_true")
    server.add_argument("--job-queue", action="store_true", help="Только локальная доверенная test/dev очередь")
    commands.add_parser("doctor", help="Проверить реальный SQL, Redis PING и версии сервисов")
    site = commands.add_parser("new-site")
    site.add_argument("site")
    site.add_argument("--testing", action="store_true")
    site.add_argument("--without-basic", action="store_true")
    execute = commands.add_parser("run", help="Выполнить команду с PATH/настройками данного bench")
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.root.resolve()
    if any(char.isspace() for char in str(root)):
        raise SystemExit("Путь runtime не должен содержать пробелы: это ограничение upstream backup/build tools")
    if root == REPO or root in REPO.parents or REPO in root.parents:
        raise SystemExit("Выберите отдельный каталог runtime вне репозитория и вне его родительских каталогов")
    if args.command == "bootstrap":
        bootstrap(root, python=args.python, postgres=not args.without_postgresql)
    else:
        os.environ.update(environment(root))
        if args.command == "serve":
            serve(root, application=not args.services_only, workers=not args.without_workers, queue=args.job_queue)
        elif args.command == "new-site":
            create_site(root, args.site, testing=args.testing, basic=not args.without_basic)
        elif args.command == "run":
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not argv:
                raise SystemExit("Укажите команду после run --")
            run(argv, cwd=root / "bench", env=environment(root))
        elif args.command == "doctor":
            print(json.dumps(Runtime(root).health(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
