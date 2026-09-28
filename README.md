# Monitor de boletas: Avengers: Doomsday

Bot independiente para esta película en **Plaza Internacional Santiago**:
https://rd.caribbeancinemas.com/plaza-internacional-santiago/movie/avengers-doomsday/

Abre la página con Chromium, ejecuta su JavaScript y comprueba la cartelera cada **60 segundos**. Envía avisos a **un número de WhatsApp**. Preparado para un servidor Linux con Docker Compose; no necesita puertos públicos.

## Qué detecta

- `unavailable`: sigue apareciendo **Nothing Scheduled**.
- `scheduled`: aparecen funciones, pero no hay botones de compra habilitados; avisa de ese cambio sin anunciar venta.
- `available`: hay al menos un botón de horario habilitado; avisa con el enlace para revisar/comprar.
- `unknown`/error: página incompleta, sucursal/película incorrecta, bloqueo, caída o estructura desconocida. Lo registra sin anunciar boletas.

Exige **dos revisiones consecutivas** antes de avisar: normalmente detecta el cambio en aproximadamente 1–2 minutos más la carga de la página. Para avisar en la primera revisión, configura `CONFIRMATIONS=1`.

El bot verifica la película y la sucursal, ignora cambios en trailers/carteles y no compra ni reserva asientos. Un botón habilitado es una señal de compra disponible, no una garantía de inventario hasta completar el proceso del cine.

## WhatsApp gratis: CallMeBot

La documentación de [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/) ofrece la API gratis **solo para uso personal**, para avisarte a tu propio número. Es un servicio de terceros, separado de la API oficial de Meta. No requiere que tu WhatsApp Web permanezca abierto. Su continuidad y tiempos de entrega dependen del proveedor; el servidor donde corre el monitor puede tener costo.

1. Abre la [guía de activación de CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/) y agrega el contacto indicado allí; usa el número publicado en esa guía por si cambia.
2. Desde el WhatsApp que recibirá las alertas, envía al contacto: `I allow callmebot to send me messages`.
3. Espera la clave de activación. El proveedor indica que, si no llega en dos minutos, se vuelva a intentar después de 24 horas.
4. Copia `.env.example` a `.env` y completa:

```dotenv
NOTIFIER=callmebot
CALLMEBOT_PHONE=+18095550123
CALLMEBOT_API_KEY=tu_clave
```

El teléfono del ejemplo es ficticio. Usa tu número con código de país, sin espacios ni guiones. No compartas la clave ni subas `.env` a Git; ya está excluido de Git y de la imagen Docker. El teléfono, el texto del aviso y la clave se transmiten a CallMeBot por HTTPS al enviar.

Si la activación de CallMeBot devuelve un identificador terminado en `@lid`, no lo uses como teléfono: en la prueba de integración su API respondió `Phone number format is incorrect`. Con el número real, la misma clave fue rechazada. Necesitas una activación válida para tu número internacional; vuelve a enviar la frase de autorización o consulta al proveedor si persiste. Cambiar el formato del identificador no corrige una clave asociada incorrectamente.

## Ejecutar en el servidor

Copia **esta carpeta completa** al servidor con Docker y el plugin Docker Compose instalados. Los comandos siguientes se ejecutan dentro de esa carpeta:

```bash
cp .env.example .env
chmod 600 .env
nano .env
docker compose build
docker compose run --rm cinema-monitor python monitor.py --check
docker compose run --rm cinema-monitor python monitor.py --test-notification
docker compose up -d
docker compose logs -f --tail=50
```

`--check` hace una lectura real sin enviar mensajes ni modificar el historial de avisos. `--test-notification` envía una prueba real; verifica que llegue antes de dejar el servidor funcionando. La API puede aceptar un mensaje sin que llegue inmediatamente.

```bash
docker compose ps
docker compose stop
docker compose start
```

`restart: unless-stopped` reinicia el proceso tras fallos y reinicios del servidor, siempre que Docker arranque al iniciar el sistema. `stop` lo detiene intencionalmente. El volumen `monitor-data` conserva el historial. **No ejecutes `docker compose down -v`** si quieres conservar la deduplicación.

## Estado, reintentos y mantenimiento

- Envía como máximo un aviso de funciones publicadas y otro de compra habilitada. Si la primera detección ya permite comprar, solo envía ese aviso. Los nuevos horarios no producen avisos adicionales.
- Guarda `state.json` antes/después del envío; solo marca el aviso como enviado cuando el proveedor confirma su aceptación.
- Si el envío falla, reintenta cada cinco minutos mientras persista el estado detectado. Las lecturas continúan cada minuto. No reenvía una señal que ya desapareció.
- Puede ocurrir un duplicado excepcional si el proveedor acepta el mensaje y se pierde la respuesta, o si el proceso cae antes de guardar la aceptación. No hay garantía de entrega exactamente una vez ni comprobación de lectura.
- `last-observation.json` conserva la última lectura reconocida, sin credenciales.
- Un bloqueo de archivo evita dos procesos usando el mismo historial.
- Docker muestra `unhealthy` si no se logra leer la cartelera durante cinco minutos o hay un envío fallido. Docker Compose no reinicia automáticamente por `unhealthy`; revisa los logs. No hay una alerta externa independiente si el servidor se apaga.
- Si cambia la estructura del sitio, puede requerirse actualizar los selectores de `monitor.py`. El bot no interpreta una página vacía como venta.

Para ver el estado persistido:

```bash
docker compose exec cinema-monitor cat /app/data/state.json
docker compose exec cinema-monitor cat /app/data/last-observation.json
```

## Alternativa: API oficial de Meta

Sí permite envíos automáticos. Para una alerta proactiva que puede llegar días después, se utiliza una **plantilla aprobada**, que puede generar cargos según la categoría y el país del destinatario. Los mensajes de servicio dentro de las 24 horas posteriores a un mensaje del usuario tienen condiciones gratuitas; eso no garantiza que esta alerta futura sea gratis. Consulta [precios oficiales](https://business.whatsapp.com/products/platform-pricing) y [documentación de WhatsApp](https://developers.facebook.com/documentation/business-messaging/whatsapp/overview).

El código también admite `NOTIFIER=meta`. Necesitas una cuenta de WhatsApp Business Platform, un número emisor configurado, token con permiso de mensajería y una plantilla aprobada. Configura las variables `META_*` de `.env.example`. Usa una versión vigente de Graph API indicada por tu app de Meta y el código de idioma exacto de tu plantilla. `META_TO` contiene un único número con código de país.

La plantilla debe tener **un parámetro de texto en el cuerpo**, sin encabezado ni botones que requieran parámetros. Ejemplo para solicitar aprobación (no garantiza aprobación ni categoría):

```text
Actualización de tu seguimiento de cartelera: {{1}}
Abre el enlace incluido para consultar los horarios.
```

El bot sustituye `{{1}}` por película, cine, estado y enlace. Un token temporal de prueba no sirve para dejarlo funcionando durante meses; configura un token adecuado y vigila su vigencia. No incluye webhook para verificar entregas posteriores a la aceptación de Meta.

## Ejecutar localmente sin Docker

Python 3.11 o superior, Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env
# Completa .env antes del envío de prueba o del arranque continuo.
.venv/bin/python monitor.py --check
.venv/bin/python monitor.py --test-notification
.venv/bin/python monitor.py
```

En Linux sin las bibliotecas de Chromium, ejecuta `playwright install --with-deps chromium` desde el entorno virtual con los permisos necesarios. Para que no dependa de tu sesión de terminal, usa el despliegue Docker anterior.

## Pruebas

```bash
.venv/bin/python -m unittest -v
```

Cubren detección, estados incompletos, confirmaciones, errores de lectura/envío, reintentos, persistencia, deduplicación, salud y las solicitudes a ambos proveedores con respuestas simuladas. No envían WhatsApps reales.

La configuración por sí sola no despliega el bot: faltan un servidor y la activación/clave de tu número. La lectura de la página sí puede probarse antes de configurar WhatsApp.
