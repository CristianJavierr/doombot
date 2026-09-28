"""Monitor de cartelera y alertas personales de WhatsApp. Python 3.11+."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("cinema")
DEFAULT_URL = "https://rd.caribbeancinemas.com/plaza-internacional-santiago/movie/avengers-doomsday/"
NO_SHOWINGS = re.compile(r"nothing\s+scheduled|nada\s+programado|no\s+hay\s+funciones", re.I)
STOP = threading.Event()


@dataclass(frozen=True)
class Config:
    url: str = DEFAULT_URL
    title: str = "Avengers: Doomsday"
    cinema: str = "Plaza Internacional Santiago"
    interval: int = 60
    confirmations: int = 2
    timeout: int = 40
    retry: int = 300
    data_dir: Path = Path("data")
    notifier: str = "callmebot"

    @classmethod
    def from_env(cls):
        config = cls(
            url=os.getenv("MOVIE_URL", DEFAULT_URL),
            title=os.getenv("EXPECTED_TITLE", cls.title),
            cinema=os.getenv("EXPECTED_CINEMA", cls.cinema),
            interval=int(os.getenv("CHECK_INTERVAL_SECONDS", "60")),
            confirmations=int(os.getenv("CONFIRMATIONS", "2")),
            timeout=int(os.getenv("PAGE_TIMEOUT_SECONDS", "40")),
            retry=int(os.getenv("NOTIFY_RETRY_SECONDS", "300")),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            notifier=os.getenv("NOTIFIER", "callmebot").lower(),
        )
        if min(config.interval, config.confirmations, config.timeout, config.retry) < 1:
            raise ValueError("Los intervalos y CONFIRMATIONS deben ser positivos.")
        if not config.title.strip() or not config.cinema.strip():
            raise ValueError("EXPECTED_TITLE y EXPECTED_CINEMA son obligatorios.")
        if urlsplit(config.url).scheme != "https":
            raise ValueError("MOVIE_URL debe usar HTTPS.")
        return config


@dataclass(frozen=True)
class Observation:
    status: str  # unavailable, scheduled, available, unknown
    detail: str


def classify(snapshot: dict, config: Config) -> Observation:
    """La ausencia de texto por sí sola nunca demuestra disponibilidad."""
    if snapshot.get("title", "").strip().casefold() != config.title.strip().casefold():
        return Observation("unknown", "No se pudo verificar la película.")
    if config.cinema.casefold() not in snapshot.get("cinema", "").casefold():
        return Observation("unknown", "No se pudo verificar la sucursal.")
    if not snapshot.get("section") or snapshot.get("loading"):
        return Observation("unknown", "Cartelera ausente o todavía cargando.")
    text = " ".join(snapshot.get("text", "").split())
    buttons = snapshot.get("buttons", [])
    if NO_SHOWINGS.search(text):
        if buttons:
            return Observation("unknown", "Cartelera con señales contradictorias.")
        return Observation("unavailable", "Nothing Scheduled: sin funciones publicadas.")
    if any(button.get("enabled") is True for button in buttons):
        return Observation("available", text[:900])
    if buttons:
        return Observation("scheduled", text[:900])
    return Observation("unknown", "Cambió el contenido, pero no hay horarios reconocibles.")


# Selectores verificados en la página real de Avengers y en la de Tuner.
SNAPSHOT_JS = """() => {
  const movie = document.querySelector('[data-test-id="movie-page"]');
  const section = movie?.querySelector('[data-test-id="showtimes-by-film"]');
  const visible = el => !!(el && el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden');
  const buttons = Array.from(section?.querySelectorAll(
    '[data-test-id^="showtimes-button-"]') || []).filter(visible);
  return {
    title: movie?.querySelector('h1')?.innerText || '',
    cinema: movie?.querySelector('[data-test-id="site-selector-filter-banner"]')?.innerText || '',
    section: visible(section),
    text: section?.innerText || '',
    loading: Array.from(movie?.querySelectorAll('.q-spinner, [aria-busy="true"]') || []).some(visible),
    buttons: buttons.map(el => ({
      text: el.innerText,
      enabled: el.getAttribute('data-test-id').endsWith('-enabled') &&
        !el.disabled && el.getAttribute('aria-disabled') !== 'true' &&
        !el.classList.contains('disabled')
    }))
  };
}"""


class PageReader:
    def __init__(self, config):
        from playwright.sync_api import sync_playwright

        self.config = config
        self.runtime = sync_playwright().start()
        self.browser = None

    def read(self):
        config = self.config
        if self.browser is None or not self.browser.is_connected():
            self.browser = self.runtime.chromium.launch(headless=True)
        context = self.browser.new_context(locale="en-US", service_workers="block")
        page = context.new_page()
        page.set_default_timeout(config.timeout * 1000)
        # No descarga carteles, fuentes ni trailers; sí JavaScript y datos de horarios.
        page.route("**/*", lambda route: route.abort() if route.request.resource_type
                   in {"image", "media", "font"} else route.continue_())
        try:
            deadline = time.monotonic() + config.timeout
            response = page.goto(config.url, wait_until="domcontentloaded")
            if response is None or response.status != 200:
                raise RuntimeError("La página no respondió HTTP 200.")
            if page.url.rstrip("/") != config.url.rstrip("/"):
                raise RuntimeError("La página redirigió a otra dirección.")
            observation = Observation("unknown", "La página no terminó de cargar.")
            stable_since = None
            previous = None
            while time.monotonic() < deadline and not STOP.is_set():
                snapshot = page.evaluate(SNAPSHOT_JS)
                observation = classify(snapshot, config)
                key = json.dumps(snapshot, sort_keys=True)
                if key != previous:
                    stable_since = time.monotonic()
                    previous = key
                # Evita tomar el estado provisional durante el renderizado de Vue.
                if observation.status != "unknown" and time.monotonic() - stable_since >= 2:
                    atomic_json(config.data_dir / "last-observation.json", snapshot)
                    return observation
                page.wait_for_timeout(500)
            raise RuntimeError(observation.detail)
        finally:
            context.close()

    def close(self):
        if self.browser:
            self.browser.close()
        self.runtime.stop()


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def load_state(config, resume_candidate=False, now=None):
    path = config.data_dir / "state.json"
    if not path.exists():
        return {"url": config.url, "sent": [], "candidate": None, "count": 0}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("sent"), list):
        raise ValueError("state.json inválido; revisar antes de reiniciar para evitar avisos duplicados.")
    if state.get("url") != config.url:
        raise ValueError("El estado pertenece a otra URL; usa otro DATA_DIR.")
    # --once puede reanudar confirmaciones de una ejecución programada reciente.
    age = (time.time() if now is None else now) - state.get("last_success", 0)
    if not resume_candidate or not 0 <= age <= config.interval * 3:
        state.update(candidate=None, count=0)
    return state


def advance(state, observation, confirmations):
    status = observation.status
    if status not in {"scheduled", "available"}:
        state.update(candidate=None, count=0)
        return None
    count = state.get("count", 0) + 1 if state.get("candidate") == status else 1
    state.update(candidate=status, count=count)
    if "available" in state["sent"] or status in state["sent"] or count < confirmations:
        return None
    return status


def alert_message(config, observation):
    if observation.status == "available":
        lead = "¡Hay horarios con botón de compra habilitado! Revisa las boletas disponibles."
    else:
        lead = "Ya aparecen funciones, pero los botones de compra aún no están habilitados."
    return f"{config.title} — {config.cinema}\n{lead}\n{config.url}"


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Falta configurar {name} en .env.")
    return value


def validate_notifier(config):
    fields = {
        "callmebot": ["CALLMEBOT_PHONE", "CALLMEBOT_API_KEY"],
        "meta": ["META_ACCESS_TOKEN", "META_PHONE_NUMBER_ID", "META_TO",
                 "META_GRAPH_VERSION", "META_TEMPLATE_NAME", "META_TEMPLATE_LANGUAGE"],
    }
    if config.notifier not in fields:
        raise ValueError("NOTIFIER debe ser callmebot o meta.")
    for name in fields[config.notifier]:
        required_env(name)
    phone = required_env("CALLMEBOT_PHONE" if config.notifier == "callmebot" else "META_TO")
    if config.notifier == "callmebot" and phone.endswith("@lid"):
        raise ValueError("CallMeBot devolvió un ID @lid, pero su API no acepta ese formato. Necesitas una activación válida para tu número internacional.")
    if not re.fullmatch(r"\+?[1-9]\d{7,14}", phone):
        raise ValueError("Número inválido: usa código de país y dígitos, sin espacios.")
    if config.notifier == "meta":
        if not re.fullmatch(r"v\d+\.\d+", required_env("META_GRAPH_VERSION")):
            raise ValueError("META_GRAPH_VERSION debe tener formato vNN.0.")
        if not required_env("META_PHONE_NUMBER_ID").isdigit():
            raise ValueError("META_PHONE_NUMBER_ID debe ser numérico.")


class NotificationError(RuntimeError):
    """Error del proveedor con mensaje seguro: sin claves, teléfonos ni URLs privadas."""


def http_request(request):
    try:
        with urlopen(request, timeout=20) as response:
            return response.read(128_000).decode("utf-8")
    except HTTPError as error:
        # No imprimir URL, cuerpo ni exception: pueden incluir claves y teléfonos.
        raise NotificationError(f"Proveedor rechazó el envío: HTTP {error.code}.") from None
    except (URLError, TimeoutError, OSError):
        raise NotificationError("Fallo de red al enviar; se reintentará. Entrega incierta.") from None


def send_notification(config, message):
    if config.notifier == "callmebot":
        query = urlencode({"phone": required_env("CALLMEBOT_PHONE"), "text": message,
                           "apikey": required_env("CALLMEBOT_API_KEY")})
        response = http_request(Request("https://api.callmebot.com/whatsapp.php?" + query))
        # CallMeBot también devuelve errores dentro de respuestas HTTP 200.
        plain = re.sub(r"<[^>]+>", " ", response).lower()
        if re.search(r"\b(error|failed|invalid|not sent|not activated)\b", plain):
            raise NotificationError("CallMeBot rechazó el mensaje; revisa activación y clave.")
        if not re.search(r"message\s+(?:has been\s+)?(?:queued|sent)|successfully\s+sent", plain):
            raise NotificationError("Respuesta no reconocida de CallMeBot; entrega no confirmada.")
    else:
        version = required_env("META_GRAPH_VERSION")
        phone_id = required_env("META_PHONE_NUMBER_ID")
        # Plantilla aprobada con un único parámetro de texto {{1}} en el cuerpo.
        payload = {
            "messaging_product": "whatsapp", "to": required_env("META_TO").lstrip("+"),
            "type": "template", "template": {
                "name": required_env("META_TEMPLATE_NAME"),
                "language": {"code": required_env("META_TEMPLATE_LANGUAGE")},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": " ".join(message.split())}]}],
            },
        }
        request = Request(f"https://graph.facebook.com/{version}/{phone_id}/messages",
                          data=json.dumps(payload).encode(), method="POST", headers={
                              "Authorization": "Bearer " + required_env("META_ACCESS_TOKEN"),
                              "Content-Type": "application/json"})
        response = json.loads(http_request(request))
        if not response.get("messages", [{}])[0].get("id"):
            raise NotificationError("Meta no confirmó la aceptación del mensaje.")


def run_cycle(config, state, reader, sender=send_notification, now=None):
    now = time.time() if now is None else now
    try:
        observation = reader.read()
    except Exception as error:
        state.update(candidate=None, count=0, last_check=now, last_status="error")
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        atomic_json(config.data_dir / "state.json", state)
        LOG.error("No se pudo leer la cartelera (%s); errores consecutivos=%s.",
                  type(error).__name__, state["consecutive_errors"])
        return False
    state.update(last_check=now, last_success=now, last_status=observation.status,
                 consecutive_errors=0)
    event = advance(state, observation, config.confirmations)
    # Guarda la observación antes del envío; solo marca sent cuando el proveedor acepta.
    atomic_json(config.data_dir / "state.json", state)
    LOG.info("Estado=%s; confirmaciones=%s.", observation.status, state.get("count", 0))
    if event and now >= state.get("retry_after", 0):
        try:
            sender(config, alert_message(config, observation))
        except Exception as error:
            state.update(retry_after=now + config.retry, notification_error=True)
            atomic_json(config.data_dir / "state.json", state)
            LOG.error("Aviso no confirmado (%s); reintento en %ss.", str(error) if isinstance(error, NotificationError) else type(error).__name__, config.retry)
            return False
        state["sent"].append(event)
        state.update(retry_after=0, notification_error=False, notification_accepted_at=now)
        atomic_json(config.data_dir / "state.json", state)
        LOG.info("Proveedor aceptó el aviso de %s.", event)
    return not state.get("notification_error", False)


def healthcheck(config):
    try:
        state = json.loads((config.data_dir / "state.json").read_text())
        fresh = time.time() - state.get("last_success", 0) < max(300, config.interval * 4)
        return 0 if fresh and not state.get("notification_error") else 1
    except (OSError, ValueError, TypeError):
        return 1


def main():
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Revisar una vez sin enviar ni cambiar estado de avisos")
    group.add_argument("--once", action="store_true", help="Revisar, notificar si corresponde, guardar estado y terminar")
    group.add_argument("--test-notification", action="store_true", help="Enviar un mensaje de prueba real")
    group.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    config = Config.from_env()
    if args.healthcheck:
        return healthcheck(config)
    if not args.check:
        validate_notifier(config)
    if args.test_notification:
        send_notification(config, f"Prueba del monitor de {config.title}. Esta prueba no indica venta de boletas. {config.url}")
        LOG.info("Proveedor aceptó el mensaje de prueba; comprueba su recepción en WhatsApp.")
        return 0
    config.data_dir.mkdir(parents=True, exist_ok=True)
    with (config.data_dir / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Ya hay un monitor usando este DATA_DIR.") from None
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        reader = PageReader(config)
        try:
            if args.check:
                print(json.dumps(asdict(reader.read()), ensure_ascii=False, indent=2))
                return 0
            state = load_state(config, resume_candidate=args.once)
            if args.once:
                return 0 if run_cycle(config, state, reader) else 1
            LOG.info("Monitor iniciado: intervalo=%ss, confirmaciones=%s, proveedor=%s.",
                     config.interval, config.confirmations, config.notifier)
            while not STOP.is_set():
                started = time.monotonic()
                run_cycle(config, state, reader)
                STOP.wait(max(0, config.interval - (time.monotonic() - started)))
        finally:
            reader.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NotificationError as error:
        LOG.error("WhatsApp: %s", error)
        sys.exit(1)
    except ValueError as error:
        LOG.error("Configuración: %s", error)
        sys.exit(1)
    except Exception as error:
        LOG.error("El monitor terminó por %s. Revisa dependencias, estado y conectividad.", type(error).__name__)
        sys.exit(1)
