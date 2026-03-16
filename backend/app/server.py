from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from app.core.llm import load_llm_config
from app.shared.pdf.extractors import PdfRouter

try:
    import redis
except Exception:  # pragma: no cover
    redis = None

BASE_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = BASE_DIR / "backend"
ARTIFACTS_DIR = BACKEND_DIR / "artifacts"
UPLOAD_DIR = BACKEND_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env")

REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_QUEUE_KEY = os.getenv("REDIS_QUEUE_KEY", "prospectus:task_queue").strip()
MODULES_PARALLEL_DEFAULT = os.getenv("MODULES_PARALLEL", "true").strip().lower() in {"1", "true", "yes", "on"}
RUNTIME_MODULES_PARALLEL = MODULES_PARALLEL_DEFAULT
TASKS_FILE = ARTIFACTS_DIR / "tasks.json"

MODULE_CMDS = {
    "price_fluctuation": "app.modules.price_fluctuation_langchain.pipeline.run_price_fluctuation_langchain",
    "shareholder_5pct": "app.modules.shareholder_5pct.pipeline.run_shareholder_5pct_langchain",
    "pledge_freeze_decl": "app.modules.pledge_freeze_langchain.pipeline.run_pledge_freeze_langchain",
}

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()
MEM_Q: queue.Queue[str] = queue.Queue()
RUNNING_PROCS: dict[tuple[str, str], subprocess.Popen] = {}
RUNNING_PROCS_LOCK = threading.Lock()

REDIS_CLIENT = None
if REDIS_URL and redis is not None:
    try:
        REDIS_CLIENT = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        REDIS_CLIENT.ping()
    except Exception:
        REDIS_CLIENT = None


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _load_tasks() -> None:
    if TASKS_FILE.exists():
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                TASKS.update(data)
        except Exception:
            pass


def _save_tasks() -> None:
    TASKS_FILE.write_text(json.dumps(TASKS, ensure_ascii=False, indent=2), encoding="utf-8")


def _enqueue(task_id: str) -> None:
    if REDIS_CLIENT is not None:
        REDIS_CLIENT.rpush(REDIS_QUEUE_KEY, task_id)
    else:
        MEM_Q.put(task_id)


def _dequeue() -> str | None:
    if REDIS_CLIENT is not None:
        item = REDIS_CLIENT.blpop(REDIS_QUEUE_KEY, timeout=5)
        return item[1] if item else None
    try:
        return MEM_Q.get(timeout=1)
    except queue.Empty:
        return None


def _mark_done_dequeue() -> None:
    if REDIS_CLIENT is None:
        MEM_Q.task_done()


def _parse_modules(raw: str | None) -> list[str]:
    if not raw:
        return ["price_fluctuation"]

    def _to_list(v) -> list[str]:
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if (s.startswith("[") and s.endswith("]")) or (s.startswith('"') and s.endswith('"')):
                try:
                    parsed = json.loads(s)
                    return _to_list(parsed)
                except Exception:
                    pass
            return [x.strip().strip('"').strip("'") for x in s.split(",") if x.strip()]
        return []

    arr = _to_list(raw)
    out = [m for m in arr if m in MODULE_CMDS]
    return out or ["price_fluctuation"]


def _ensure_shared_preprocessed(task: dict) -> str | None:
    try:
        root = Path(task["workdir"])
        root.mkdir(parents=True, exist_ok=True)
        p = root / "preprocessed_shared.json"
        if p.exists():
            return str(p)
        router = PdfRouter()
        text_blocks, table_blocks = router.extract(task["pdf_path"])
        payload = {
            "text_blocks": [x.model_dump() for x in text_blocks],
            "table_blocks": [x.model_dump() for x in table_blocks],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(p)
    except Exception:
        return None


def _run_module(task: dict, module: str) -> tuple[bool, str, str | None]:
    mod_workdir = Path(task["workdir"]) / module
    mod_workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        MODULE_CMDS[module],
        "--pdf",
        task["pdf_path"],
        "--workdir",
        str(mod_workdir),
    ]
    shared_pre = task.get("preprocessed_shared")
    if shared_pre:
        cmd.extend(["--preprocessed", str(shared_pre)])

    proc = subprocess.Popen(cmd, cwd=str(BACKEND_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with RUNNING_PROCS_LOCK:
        RUNNING_PROCS[(task["id"], module)] = proc

    try:
        stdout, stderr = proc.communicate()
    finally:
        with RUNNING_PROCS_LOCK:
            RUNNING_PROCS.pop((task["id"], module), None)

    log_text = (stdout or "") + "\n" + (stderr or "")
    rp = mod_workdir / "result.json"
    ok = proc.returncode == 0 and rp.exists()
    return ok, log_text, str(rp) if ok else None


def _worker() -> None:
    while True:
        task_id = _dequeue()
        if not task_id:
            continue
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            if not task:
                _mark_done_dequeue()
                continue
            if task.get("status") == "cancelled":
                _mark_done_dequeue()
                continue
            task["status"] = "running"
            task["updated_at"] = _now()
            _save_tasks()

        try:
            with TASKS_LOCK:
                task = TASKS[task_id]
                selected = [m for m in (task.get("selected_modules") or []) if m in task.get("modules", {})]
                for m in selected:
                    mod = task["modules"][m]
                    if mod.get("status") not in {"success", "cancelled"}:
                        mod["status"] = "running"
                        mod["updated_at"] = _now()
                _save_tasks()

            shared_pre = _ensure_shared_preprocessed(TASKS[task_id])
            with TASKS_LOCK:
                if task_id in TASKS:
                    TASKS[task_id]["preprocessed_shared"] = shared_pre
                    _save_tasks()

            results: dict[str, tuple[bool, str, str | None]] = {}

            def _runner(module_name: str) -> None:
                with TASKS_LOCK:
                    t = TASKS.get(task_id)
                    if not t:
                        return
                    st = t["modules"].get(module_name, {}).get("status")
                    if st in {"success", "cancelled"} or t.get("status") == "cancelled":
                        return
                ok, log_text, rp = _run_module(TASKS[task_id], module_name)
                results[module_name] = (ok, log_text, rp)

            run_parallel = bool(task.get("modules_parallel", RUNTIME_MODULES_PARALLEL))
            if run_parallel:
                threads: list[threading.Thread] = []
                for m in selected:
                    t = threading.Thread(target=_runner, args=(m,), daemon=True)
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join()
            else:
                for m in selected:
                    _runner(m)

            with TASKS_LOCK:
                task = TASKS[task_id]
                for m, out in results.items():
                    ok, log_text, rp = out
                    mod = task["modules"][m]
                    mod["log"] = (mod.get("log") or "") + log_text
                    if mod.get("status") != "cancelled":
                        mod["status"] = "success" if ok else "failed"
                    mod["updated_at"] = _now()
                    if rp:
                        mod["result_path"] = rp
                statuses = [task["modules"][m]["status"] for m in task.get("selected_modules", []) if m in task.get("modules", {})]
                if statuses and all(s == "success" for s in statuses):
                    task["status"] = "success"
                elif any(s == "cancelled" for s in statuses):
                    task["status"] = "cancelled"
                else:
                    task["status"] = "failed"
                task["updated_at"] = _now()
                _save_tasks()
        finally:
            _mark_done_dequeue()


_load_tasks()
threading.Thread(target=_worker, daemon=True).start()


@app.get("/api/v1/health")
def health():
    cfg = load_llm_config()
    return jsonify(
        {
            "ok": True,
            "runtime": "langchain-refactor",
            "llm_configured": bool(cfg.api_key),
            "llm_model": cfg.model,
            "llm_base_url": cfg.base_url,
            "modules_parallel": RUNTIME_MODULES_PARALLEL,
            "redis_enabled": REDIS_CLIENT is not None,
        }
    )


@app.get("/api/v1/runtime")
def get_runtime():
    return jsonify({"modules_parallel": RUNTIME_MODULES_PARALLEL})


@app.post("/api/v1/runtime")
def set_runtime():
    global RUNTIME_MODULES_PARALLEL
    body = request.get_json(silent=True) or {}
    val = body.get("modules_parallel")
    if isinstance(val, bool):
        RUNTIME_MODULES_PARALLEL = val
    elif isinstance(val, str):
        RUNTIME_MODULES_PARALLEL = val.strip().lower() in {"1", "true", "yes", "on", "parallel"}
    return jsonify({"ok": True, "modules_parallel": RUNTIME_MODULES_PARALLEL})


@app.get("/api/v1/results")
def list_results():
    items = []
    for rp in sorted(ARTIFACTS_DIR.rglob("result.json"), reverse=True):
        if "uploads" in str(rp):
            continue
        module = rp.parent.name
        task_dir = rp.parent.parent.name if rp.parent.parent else ""
        items.append(
            {
                "name": f"{task_dir}/{module}" if task_dir else rp.parent.name,
                "workdir": str(rp.parent),
                "result_path": str(rp),
                "module": module,
            }
        )
    return jsonify({"items": items})


@app.get("/api/v1/result")
def get_result():
    p = Path(request.args.get("path", ""))
    if not p.exists():
        return jsonify({"error": "result not found"}), 404
    return jsonify(json.loads(p.read_text(encoding="utf-8")))


@app.get("/api/v1/file")
def get_file():
    p = Path(request.args.get("path", ""))
    if not p.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(p))


@app.post("/api/v1/tasks")
def create_task():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "missing file"}), 400

    modules_raw = request.form.get("modules")
    modules = _parse_modules(modules_raw)
    mp_raw = (request.form.get("modules_parallel") or "").strip().lower()
    if mp_raw in {"true", "1", "yes", "on", "parallel"}:
        modules_parallel = True
    elif mp_raw in {"false", "0", "no", "off", "serial"}:
        modules_parallel = False
    else:
        modules_parallel = RUNTIME_MODULES_PARALLEL

    filename = f.filename or f"upload_{uuid.uuid4().hex}.pdf"
    saved_pdf = UPLOAD_DIR / f"{uuid.uuid4().hex}_{filename}"
    f.save(saved_pdf)

    task_id = uuid.uuid4().hex
    workdir = str(ARTIFACTS_DIR / f"task_{task_id}")
    mod_state = {
        m: {
            "status": "queued",
            "result_path": None,
            "log": "",
            "updated_at": _now(),
        }
        for m in modules
    }

    task = {
        "id": task_id,
        "filename": filename,
        "pdf_path": str(saved_pdf),
        "status": "queued",
        "workdir": workdir,
        "selected_modules": modules,
        "request_modules_raw": modules_raw,
        "modules_parallel": modules_parallel,
        "modules": mod_state,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with TASKS_LOCK:
        TASKS[task_id] = task
        _save_tasks()
    _enqueue(task_id)
    return jsonify(task)


@app.post("/api/v1/results/batch-delete")
def batch_delete_results():
    body = request.get_json(silent=True) or {}
    paths = body.get("result_paths") or []
    if not isinstance(paths, list):
        return jsonify({"error": "result_paths must be a list"}), 400

    deleted: list[str] = []
    skipped: list[dict] = []

    for raw in paths:
        p = Path(str(raw or "")).resolve()
        try:
            p.relative_to(ARTIFACTS_DIR.resolve())
        except Exception:
            skipped.append({"path": str(raw), "reason": "out_of_artifacts"})
            continue

        # 支持传 task_root 或 result.json 路径
        task_root = None
        p_str = str(p).replace("\\", "/")
        if "/task_" in p_str:
            if p.name == "result.json" and p.parent.parent.name.startswith("task_"):
                task_root = p.parent.parent
            elif p.name.startswith("task_") and p.is_dir():
                task_root = p
            else:
                for parent in p.parents:
                    if parent.name.startswith("task_"):
                        task_root = parent
                        break

        if task_root and task_root.exists() and task_root.is_dir():
            shutil.rmtree(task_root, ignore_errors=True)
            deleted.append(str(task_root))
            task_root_str = str(task_root)
            with TASKS_LOCK:
                for tid, t in list(TASKS.items()):
                    if str(t.get("workdir", "")) == task_root_str and t.get("status") in {"success", "failed", "cancelled"}:
                        TASKS.pop(tid, None)
                _save_tasks()
            continue

        if p.exists() and p.is_file() and p.name == "result.json":
            try:
                p.unlink()
                deleted.append(str(p))
            except Exception:
                skipped.append({"path": str(raw), "reason": "unlink_failed"})
        else:
            skipped.append({"path": str(raw), "reason": "not_found"})

    return jsonify({"ok": True, "deleted": deleted, "skipped": skipped})


@app.post("/api/v1/tasks/<task_id>/cancel")
def cancel_task(task_id: str):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404

        selected = [m for m in task.get("selected_modules", []) if m in task.get("modules", {})]
        for m in selected:
            mod = task["modules"][m]
            if mod.get("status") in {"queued", "running"}:
                mod["status"] = "cancelled"
                mod["updated_at"] = _now()
                mod["log"] = (mod.get("log") or "") + "\nCancelled by user.\n"

        task["status"] = "cancelled"
        task["updated_at"] = _now()
        _save_tasks()

    killed = []
    with RUNNING_PROCS_LOCK:
        for (tid, module), proc in list(RUNNING_PROCS.items()):
            if tid != task_id:
                continue
            try:
                proc.terminate()
            except Exception:
                pass
            killed.append(module)

    return jsonify({"ok": True, "task_id": task_id, "killed_modules": killed})


@app.get("/api/v1/tasks")
def list_tasks():
    with TASKS_LOCK:
        items = sorted(TASKS.values(), key=lambda x: x["created_at"], reverse=True)
    return jsonify({"items": items})


@app.get("/api/v1/tasks/<task_id>")
def get_task(task_id: str):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
    return jsonify(task)


@app.delete("/api/v1/tasks/<task_id>")
def delete_task(task_id: str):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if task.get("status") not in {"success", "failed", "cancelled"}:
            return jsonify({"error": "only finished tasks can be deleted"}), 400
        TASKS.pop(task_id, None)
        _save_tasks()
    return jsonify({"ok": True, "deleted": [task_id]})


@app.post("/api/v1/tasks/batch-delete")
def batch_delete_tasks():
    body = request.get_json(silent=True) or {}
    ids = body.get("task_ids") or []
    if not isinstance(ids, list):
        return jsonify({"error": "task_ids must be a list"}), 400

    deleted: list[str] = []
    skipped: list[dict] = []

    with TASKS_LOCK:
        for raw_id in ids:
            task_id = str(raw_id or "").strip()
            if not task_id:
                continue
            task = TASKS.get(task_id)
            if not task:
                skipped.append({"id": task_id, "reason": "not_found"})
                continue
            if task.get("status") not in {"success", "failed", "cancelled"}:
                skipped.append({"id": task_id, "reason": "not_finished"})
                continue
            TASKS.pop(task_id, None)
            deleted.append(task_id)

        _save_tasks()

    return jsonify({"ok": True, "deleted": deleted, "skipped": skipped})


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "9010"))
    app.run(host="0.0.0.0", port=port, debug=False)
