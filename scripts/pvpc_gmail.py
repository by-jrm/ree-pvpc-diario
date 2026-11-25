#!/usr/bin/env python3
"""Envía el PNG publicado y conserva una reserva remota por fecha oficial."""
import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import date
from email.message import EmailMessage
from pathlib import PurePosixPath

SENDER = "universal.jrm.2010@gmail.com"
RECIPIENT = "claudia.velenich@gmail.com"
REPORT = "data/latest.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SECRET_NAMES = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


class DeliveryError(Exception):
    """Error que puede aparecer en el registro de Actions."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def git(*args):
    # No propagar las credenciales de Gmail a Git ni mostrar sus diagnósticos.
    env = {key: value for key, value in os.environ.items() if key not in SECRET_NAMES}
    try:
        return subprocess.run(
            ["git", *args], env=env, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=45,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise DeliveryError("No se pudo comprobar o actualizar el estado en Git.") from None


def official_date(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Fecha no canónica")
    return parsed


def published_report():
    git("fetch", "--no-tags", "origin", "refs/heads/main")
    commit = git("rev-parse", "FETCH_HEAD").decode().strip()
    data = git("cat-file", "blob", f"{commit}:{REPORT}")
    if data != git("cat-file", "blob", f"HEAD:{REPORT}"):
        raise DeliveryError("El JSON local y el publicado no coinciden.")
    report = json.loads(data)
    if (report["verificacion"] != "official_verified"
            or report["indicador_esios"] != 1001 or report["geo_id"] != 8741):
        raise DeliveryError("El JSON publicado no acredita la validación oficial.")
    return official_date(report["fecha"]), commit


def published_png(day, commit):
    path = f"data/infografias/precio_electricidad_pvpc_{day.isoformat()}.png"
    content = git("cat-file", "blob", f"{commit}:{path}")
    if (not content.startswith(b"\x89PNG\r\n\x1a\n")
            or content != git("cat-file", "blob", f"HEAD:{path}")):
        raise DeliveryError("El PNG publicado no coincide con la imagen local.")
    return PurePosixPath(path).name, content


def message_body(day, filename, png):
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = RECIPIENT
    message["Subject"] = f"VELILLA PRECIO ELECTRICIDAD | {day:%d/%m/%Y}"
    message["Message-ID"] = f"<velilla-pvpc-{day.isoformat()}@users.noreply.github.com>"
    message.set_content(f"Adjunto el precio de la electricidad para el {day:%d/%m/%Y}.")
    message.add_attachment(png, maintype="image", subtype="png", filename=filename)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return json.dumps({"raw": raw}).encode()


def post(url, data, headers):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def refresh_token():
    client_id, client_secret, token = (os.environ.get(name) for name in SECRET_NAMES)
    if not all((client_id, client_secret, token)):
        raise DeliveryError("Faltan los secretos de autorización de Gmail.")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": token,
        "grant_type": "refresh_token",
    }).encode()
    try:
        result = post(TOKEN_URL, data, {"Content-Type": "application/x-www-form-urlencoded"})
        access_token = result["access_token"]
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Token inválido")
    except (OSError, KeyError, TypeError, ValueError):
        raise DeliveryError("Falló la autorización de Gmail antes del envío.") from None
    return access_token


def reserve(commit, remote_ref):
    name = remote_ref.removeprefix("refs/tags/")
    # Un objeto distinto por intento impide que dos pushes parezcan idénticos.
    git("-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "tag", "-a", "--no-sign", "-m", uuid.uuid4().hex, name, commit)
    git("push", f"--force-with-lease={remote_ref}:", "origin", f"{remote_ref}:{remote_ref}")


def deliver(start):
    day, commit = published_report()
    if day < start:
        print(f"{day}: anterior a la fecha de activación.")
        return

    reserved = f"refs/tags/pvpc-gmail/reservado/{day}"
    sent = f"refs/tags/pvpc-gmail/enviado/{day}"
    refs = git("ls-remote", "--refs", "origin", reserved, sent).decode()
    present = {line.split("\t", 1)[1] for line in refs.splitlines()}
    if sent in present:
        print(f"{day}: el envío ya está registrado.")
        return
    if reserved in present:
        raise DeliveryError(f"{day}: envío reservado. Comprueba Enviados antes de reintentar.")

    payload = message_body(day, *published_png(day, commit))
    token = refresh_token()
    reserve(commit, reserved)
    try:
        response = post(SEND_URL, payload, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        })
        if not response.get("id"):
            raise ValueError("Respuesta sin identificador")
        git("push", f"--force-with-lease={sent}:", "origin", f"{commit}:{sent}")
    except (OSError, AttributeError, TypeError, ValueError, DeliveryError):
        raise DeliveryError(
            f"{day}: resultado sin confirmar. Comprueba Enviados antes de reintentar."
        ) from None

    print(f"{day}: Gmail ha aceptado el mensaje y el envío está registrado.")


def main():
    try:
        deliver(official_date(os.environ["GMAIL_START_DATE"]))
    except DeliveryError as error:
        print(error, file=sys.stderr)
        return 1
    except (TypeError, KeyError, ValueError):
        print("La fecha de activación o el JSON oficial no son válidos.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
