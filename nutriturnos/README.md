# Turnos de nutrición

Aplicación para reservas, agenda profesional, ficha clínica y seguimiento de pacientes. La hora de la agenda se interpreta como hora de Argentina (`America/Argentina/Buenos_Aires`).

## Desarrollo local

Desde `nutriturnos/`, instalá las dependencias y configurá secretos antes de iniciar. No hay claves por defecto:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SECRET_KEY = [guid]::NewGuid().ToString("N")
$env:COOKIE_SECURE = "false"
```

La app requiere `SECRET_KEY` de al menos 32 caracteres y se niega a iniciar si falta o no cumple el mínimo. El acceso profesional es con email y contraseña; no requiere Google Cloud ni credenciales OAuth. En producción usá secretos aleatorios y únicos, no guardes credenciales en el código y dejá `COOKIE_SECURE=true` (por defecto). No uses el servidor integrado de Flask como servidor de producción.

## Base de datos y migraciones

La base SQLite por defecto está en `instance/nutri.db`. La aplicación ya no crea/modifica tablas al arrancar: aplicá las migraciones explícitamente.

Base nueva:

```powershell
flask --app app db upgrade
python app.py
```

Cada profesional crea su propia cuenta con email y contraseña (mínimo 8 caracteres). Al registrarse se generan sus servicios, horarios y configuración iniciales; no se comparten pacientes, turnos, agenda, señas ni conexión de Google Calendar con otras cuentas.

Pruebas:

```powershell
python -m unittest discover -s tests -v
```

Instalación existente que todavía usaba `db.create_all()`:

1. Detené la aplicación y hacé una copia segura del archivo `instance/nutri.db`.
2. Desde esta carpeta, marcá el esquema histórico y aplicá la migración aditiva:

```powershell
flask --app app db stamp b00000000001
flask --app app db upgrade
```

El `stamp` no altera tablas ni datos; indica que la base existente corresponde al esquema inicial. La migración conserva los registros y los asigna a una sola cuenta profesional (la más antigua si ya existe; si no, a una cuenta histórica no accesible). Las cuentas nuevas quedan aisladas. Hacé un respaldo y verificá la titularidad antes de migrar una instalación con pacientes o turnos. Si detecta turnos activos duplicados para el mismo horario, se detiene sin borrarlos; resolvé esos duplicados y volvé a ejecutar `flask --app app db upgrade`.

Las copias automáticas completas se guardan en `instance/backups/` (o en `BACKUP_DIR`) y se retienen por 14 días; el proceso de automatizaciones debe permanecer ejecutándose para generar una copia diaria. La descarga manual desde **Respaldo** contiene solamente los datos de la cuenta profesional que la solicita. Guardá copias adicionales fuera del servidor: una copia en el mismo disco no protege contra pérdida del equipo.

## Reservas, señas y contenido

- Cada profesional comparte su enlace `/p/<slug>` desde **Configuración**. La página `/` muestra el directorio público. En `/admin/config` se editan el título y la descripción pública, las consultas (descripción, duración, precio y modalidad), horarios, alias e instrucciones de pago, únicamente para esa cuenta.
- Cada profesional define su propia seña en pesos; `0` la desactiva. Se informa su alias y sus instrucciones antes de reservar.
- Si la seña continúa pendiente 24 horas desde la reserva, el turno se cancela automáticamente. En la agenda, el profesional marca manualmente la seña como pagada.
- Para reservar, el paciente ingresa un WhatsApp con código internacional; no se solicita email ni se envía un código de verificación. La confirmación del turno aparece en pantalla.
- Cada reserva incluye un enlace firmado para que el paciente pueda cancelar o reprogramar. El enlace vence al año.
- Hay límites de solicitudes por IP y por número de WhatsApp en reservas, y por IP en búsqueda de disponibilidad e inicio de sesión; también hay un campo trampa anti-bot. El inicio de sesión y todos los formularios están protegidos con CSRF. Para varios procesos web configurá un almacenamiento compartido con `RATELIMIT_STORAGE_URI` (por ejemplo Redis) en lugar del almacenamiento en memoria local.
- Las restricciones únicas de la base evitan dos reservas activas para el mismo horario, incluso ante solicitudes simultáneas. Los turnos cancelados liberan el horario.

Las reservas y la agenda requieren conexión al servidor. En navegadores compatibles, el sitio puede instalarse como aplicación web; no ofrece operación de reservas offline.

## Automatizaciones

Ejecutá el proceso de tareas en una consola o como servicio separado, además del servidor web:

```powershell
python scheduler.py
```

Ejecutá una sola instancia de este proceso: comprueba cada cinco minutos las señas vencidas y los recordatorios, y crea el respaldo diario a las 03:00 de Argentina. Si el proceso se detiene, las tareas quedan pausadas hasta que vuelva a ejecutarse.

### Email y avisos al profesional

Configurar SMTP habilita los avisos internos por email al profesional cuando se reserva un turno. El paciente no recibe emails. Para Gmail, usá una contraseña de aplicación:

```text
SMTP_USERNAME=
SMTP_PASSWORD=contraseña-de-aplicación
SMTP_SENDER=cuenta@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
```

`SMTP_SENDER` puede omitirse si `SMTP_USERNAME` está definido. En `/admin/config`, completá el email profesional para recibir avisos de nuevas reservas. La reserva se conserva aunque falle el aviso y el error queda en el log.

### Google Calendar (opcional)

Google Calendar es opcional; no se necesita para crear una cuenta ni iniciar sesión. Para habilitar la integración, crear credenciales OAuth de tipo aplicación web, habilitar Google Calendar API y registrar estas URI de redirección:

```text
https://TU-DOMINIO/admin/google/callback
https://TU-DOMINIO/admin/google/calendar/callback
```

Para probar en esta computadora, registrá también:

```text
http://127.0.0.1:5000/admin/google/callback
http://127.0.0.1:5000/admin/google/calendar/callback
```

Configurar `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET` como variables de entorno y reiniciar la app. Cada profesional puede conectar su propio calendario desde `/admin/config`; el inicio de sesión continúa usando email y contraseña. El token se guarda cifrado usando `SECRET_KEY`; si cambiás esa clave, cada profesional debe volver a autorizar Google. Si no configurás credenciales, la integración de Calendar queda deshabilitada.

### WhatsApp Cloud API

Crear en Meta las plantillas aprobadas para recordatorio de turno (4 variables: nombre, fecha, hora y consulta) y cancelación por seña vencida (3 variables). Configurar:

```text
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_APPOINTMENT_TEMPLATE=nombre_plantilla_turno
WHATSAPP_DEPOSIT_CANCEL_TEMPLATE=nombre_plantilla_cancelacion
WHATSAPP_TEMPLATE_LANGUAGE=es_AR
```

El paciente debe aceptar expresamente los avisos y registrar el teléfono con código internacional. Con la integración activa, se envían avisos 24 horas y 2 horas antes. El seguimiento mensual no se envía automáticamente: cuando pasan 30 días desde el último turno marcado como completado y no hay otro turno próximo, aparece en la agenda profesional. Franco puede abrir WhatsApp, enviar el mensaje manualmente y marcar el seguimiento como realizado. Esta función se calcula al cargar la agenda y no requiere Meta ni `scheduler.py`. Las credenciales y plantillas de los avisos automáticos se configuran a nivel de la instalación; si faltan, se registra el error y el aviso no se marca como enviado.

## Variables de entorno

| Variable | Uso |
|---|---|
| `SECRET_KEY` | Firma de sesiones, enlaces de gestión y cifrado de tokens; obligatoria |
| `DATABASE_URL` | URL SQLAlchemy; por defecto SQLite local |
| `COOKIE_SECURE` | Cookies HTTPS seguras; `true` por defecto |
| `RATELIMIT_STORAGE_URI` | Almacenamiento compartido para límites; por defecto memoria local |
| `BACKUP_DIR` | Carpeta de respaldos; por defecto `instance/backups` |
| `SMTP_*` | Envío de confirmaciones y notificaciones por email |
| `GOOGLE_*` | Integración opcional con Google Calendar |
| `WHATSAPP_*` | Plantillas y credenciales de WhatsApp Cloud API |
