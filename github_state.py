"""Persiste solo datos públicos del monitor en una rama separada de GitHub."""

import json
import math
import os
from pathlib import Path
import subprocess
import sys

from monitor import DEFAULT_URL, atomic_json

BRANCH = "codex/monitor-state"
REF = f"refs/remotes/origin/{BRANCH}"
FIELDS = {
    "url", "sent", "candidate", "count", "last_check", "last_success", "last_status",
    "consecutive_errors", "retry_after", "notification_error", "notification_accepted_at",
}
NUMBERS = {"count", "last_check", "last_success", "consecutive_errors", "retry_after",
           "notification_accepted_at"}


def safe_state(state):
    # No permite que una futura modificación publique claves, teléfonos o texto arbitrario.
    if not isinstance(state, dict) or set(state) - FIELDS:
        raise ValueError("El historial contiene campos no permitidos.")
    if state.get("url") != DEFAULT_URL:
        raise ValueError("El historial no corresponde a la película configurada.")
    if not isinstance(state.get("sent"), list) or any(
        item not in {"scheduled", "available"} for item in state["sent"]
    ):
        raise ValueError("Lista de avisos inválida.")
    if state.get("candidate") not in {None, "scheduled", "available"}:
        raise ValueError("Confirmación inválida.")
    if state.get("last_status") not in {None, "unavailable", "scheduled", "available", "unknown", "error"}:
        raise ValueError("Estado inválido.")
    for key in NUMBERS & state.keys():
        if type(state[key]) not in {int, float} or not math.isfinite(state[key]) or state[key] < 0:
            raise ValueError("Contador o fecha inválidos.")
    if "notification_error" in state and type(state["notification_error"]) is not bool:
        raise ValueError("Resultado de aviso inválido.")
    return state


def git(*args, input=None):
    return subprocess.run(
        ["git", *args], input=input, text=True, check=True, capture_output=True,
    ).stdout.strip()


def restore(directory):
    # Una rama ausente o una caída de GitHub NO equivalen a un historial vacío.
    git("fetch", "--no-tags", "origin", f"+refs/heads/{BRANCH}:{REF}")
    parent = git("rev-parse", REF)
    state = safe_state(json.loads(git("show", f"{parent}:state.json")))
    atomic_json(directory / "state.json", state)
    (directory / "state-parent").write_text(parent, encoding="ascii")


def save(directory):
    state = safe_state(json.loads((directory / "state.json").read_text(encoding="utf-8")))
    parent = (directory / "state-parent").read_text(encoding="ascii").strip()
    previous = safe_state(json.loads(git("show", f"{parent}:state.json")))
    if state == previous:
        return
    blob = git("hash-object", "-w", "--stdin", input=json.dumps(state, indent=2) + "\n")
    tree = git("mktree", input=f"100644 blob {blob}\tstate.json\n")
    commit = git(
        "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit-tree", tree, "-p", parent, "-m", "Update cinema monitoring state",
    )
    # Fast-forward exclusivamente: un cambio concurrente debe fallar, nunca sobrescribirse.
    git("push", "origin", f"{commit}:refs/heads/{BRANCH}")


def summary(directory):
    path = directory / "state.json"
    if not path.exists():
        report = "No se pudo recuperar o crear el historial; revisa el paso fallido.\n"
    else:
        state = safe_state(json.loads(path.read_text(encoding="utf-8")))
        report = (
            "### Monitor de Avengers: Doomsday\n\n"
            f"- Estado: `{state.get('last_status', 'sin revisar')}`\n"
            f"- Avisos aceptados: `{', '.join(state['sent']) or 'ninguno'}`\n"
            f"- Error pendiente de WhatsApp: `{state.get('notification_error', False)}`\n"
            "- La aceptación de la API no verifica la entrega al teléfono.\n"
        )
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as file:
            file.write(report)
    else:
        print(report)


if __name__ == "__main__":
    try:
        command = sys.argv[1]
        {"restore": restore, "save": save, "summary": summary}[command](
            Path(os.getenv("DATA_DIR", "data")))
    except Exception as error:
        print(f"Historial: falló la operación ({type(error).__name__}). No se reinició ni sobrescribió el historial.", file=sys.stderr)
        sys.exit(1)
