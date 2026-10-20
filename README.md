# Sentinel AI

Plataforma de observabilidad, diagnóstico (RCA) y reparación asistida por IA.

Este repositorio lo desarrolla de forma autónoma **AI Software Factory** (Autopilot), un ítem del backlog por rama `factory/sentinel-ai/NN-...`, con PRs en draft para revisión humana. La especificación está en [`docs/SPEC.md`](docs/SPEC.md).

## Visión general

Sentinel AI recibe las excepciones de las aplicaciones monitoreadas, las agrupa en incidentes, investiga la causa raíz con un agente de IA que lee (solo lectura) el repositorio autorizado de la aplicación, prepara una corrección en una copia aislada, la verifica ejecutando los tests del repositorio y, si pasan, prepara un pull request en **borrador**. Todo queda registrado y visible en el dashboard.

Sentinel **nunca** modifica producción, **nunca** hace merge y **nunca** despliega cambios.

```
demo-app ──(excepción, X-Sentinel-Key)──▶ POST /api/v1/ingest/events
                                              │ validación, límites, redacción
                                              ▼
                                  fingerprint ─▶ incidente (severidad, contador)
                                              │
                         POST /incidents/{id}/rca ─▶ cola de jobs ─▶ worker ─▶ motor RCA
                                              │                       (herramientas de solo lectura)
                      POST /incidents/{id}/repair ─▶ cola de jobs ─▶ worker ─▶ motor de reparación
                                              │        (copia aislada, rama sentinel/fix-<incidente>-<n>,
                                              │         parche validado, tests, reintentos)
                POST /incidents/{id}/pull-request ─▶ PR en borrador (gh, publicación desactivada por defecto)
                                              │
                                              ▼
                                   dashboard (React) vía /api
```

## Arquitectura

| Directorio | Contenido |
|---|---|
| `backend/` | API de Sentinel (FastAPI, SQLAlchemy 2, Pydantic 2). Paquete `app/`, tests en `backend/tests/`. Dependencias en `backend/requirements.txt` (versiones fijadas en `backend/requirements.lock.txt`). |
| `frontend/` | Dashboard (Vite, React 19, React Router 7, TypeScript, Tailwind 4). Gráficos en SVG plano. Tests con Vitest en `frontend/src/**/*.test.tsx`. |
| `demo-app/` | Aplicación FastAPI independiente con tres fallas intencionales y un cliente que reporta excepciones a Sentinel por HTTP. No importa nada de `backend/`. |

Módulos principales del backend:

- `app/api/`: routers HTTP (`ingest`, `admin`, `dashboard`, `jobs`, `pull_requests`, `costs`, `stats`).
- `app/ingestion.py`, `app/ingest/`: ingesta, fingerprint, agrupación, deduplicación y severidad.
- `app/security/`: autenticación (token de administrador, claves de ingesta guardadas como hash) y redacción de secretos.
- `app/ai/`: interfaz `AIProvider`, proveedor mock determinista, adaptador Anthropic y registro de proveedores.
- `app/repo_tools.py`: herramientas de lectura acotadas al repositorio autorizado.
- `app/rca/`: motor RCA y esquema del informe estructurado.
- `app/repair/`: workspace git aislado, parches validados, motor de reparación y confirmación de hipótesis.
- `app/github/`: preparación de PRs en borrador vía `gh`.
- `app/runner.py`: ejecución de comandos (argv sin shell, timeout, entorno mínimo) con allow-list de subcomandos `git`/`gh`.
- `app/jobs/`, `app/worker.py`: cola persistente de jobs y worker.

## Requisitos

- Python 3.12+ con las dependencias de `backend/requirements.txt` (las comparten `backend/` y `demo-app/`).
- Node.js 20+ para el frontend (`frontend/package.json`).
- `git` en el `PATH` (lo usa el motor de reparación para la copia aislada). `gh` solo si se activa la publicación de PRs.

## Ejecutar el backend localmente

```bash
cd backend
pip install -r requirements.txt

# Token de administrador para la API del dashboard (elegí un valor propio; sin él la API admin responde 503).
export SENTINEL_ADMIN_TOKEN="un-token-local-largo"

# Crea la base de datos, la aplicación "demo-app" y su repositorio autorizado (demo-app/).
# Imprime la clave de ingesta UNA sola vez: guardala para la demo.
python -m app.seed

# API en http://localhost:8000 (documentación interactiva en /docs).
uvicorn app.main:app --port 8000
```

En otra terminal (mismo directorio y mismas variables de entorno), iniciá el worker que procesa los jobs de RCA y reparación:

```bash
cd backend
export SENTINEL_ADMIN_TOKEN="un-token-local-largo"
python -m app.worker
```

Alternativa sin worker: con `SENTINEL_RUN_JOBS_INLINE=true` la API procesa cada job antes de responder (útil para demos y pruebas).

En PowerShell usá `$env:SENTINEL_ADMIN_TOKEN = "un-token-local-largo"` en lugar de `export`.

Tests: `python -m pytest -q` dentro de `backend/`.

## Ejecutar el frontend localmente

```bash
cd frontend
npm install        # solo la primera vez; las dependencias están fijadas
npm run dev        # http://localhost:5173
```

El servidor de desarrollo de Vite redirige `/api` al backend en `http://localhost:8000` (proxy configurado en `frontend/vite.config.ts`), así que no hace falta configurar CORS. Al abrir el dashboard, ingresá el valor de `SENTINEL_ADMIN_TOKEN` en el campo **Admin token** de la cabecera (se guarda en `localStorage` del navegador).

Verificaciones: `npm run typecheck`, `npm test` (Vitest) y `npm run build`.

## Ejecutar la aplicación de demostración localmente

```bash
cd demo-app
export DEMO_FAULTS_ENABLED=true                    # por defecto true en DEMO_ENVIRONMENT=development
export SENTINEL_URL="http://localhost:8000"         # URL base de la API de Sentinel
export SENTINEL_INGEST_KEY="<clave impresa por python -m app.seed>"
uvicorn demo_app.main:create_app --factory --port 8001
```

Variables de la demo:

| Variable | Por defecto | Descripción |
|---|---|---|
| `DEMO_ENVIRONMENT` | `development` | Entorno que se informa a Sentinel. |
| `DEMO_FAULTS_ENABLED` | `true` en `development`, `false` en otros | Habilita las fallas intencionales. La aplicación se niega a iniciar si se habilitan con `DEMO_ENVIRONMENT=production`. |
| `SENTINEL_URL` | (vacío) | URL base de Sentinel. Sin ella el reporte está desactivado. |
| `SENTINEL_INGEST_KEY` | (vacío) | Clave de ingesta de la aplicación (header `X-Sentinel-Key`). Sin ella el reporte está desactivado. |

El reporte es de mejor esfuerzo: si Sentinel no está disponible, la demo responde igual (500 en las fallas) y nunca se bloquea.

Fallas intencionales (no se corrigen en `demo-app/`; las repara Sentinel en una copia aislada):

1. `GET /users/2/profile`: el `nickname` es `None` y se usa como string → `AttributeError` → 500.
2. `POST /orders` con `{"product": "widget", "quantity": "three"}`: conversión sin validar → `ValueError` → 500 en lugar de 422.
3. `GET /orders/1/total?coupon=BIGDEAL`: un cupón fijo mayor al subtotal produce un total negativo → `DiscountError` → 500.

Tests: `python -m pytest -q` dentro de `demo-app/`. Los tests de reproducción (`@pytest.mark.fault_reproduction`) afirman las fallas; el comando que usa Sentinel para verificar reparaciones los excluye: `python -m pytest -q -m "not fault_reproduction"`.

## Recorrido: de una falla al PR en borrador

Con el backend, el worker, el frontend y la demo corriendo (ver arriba):

1. **Provocar la falla.**
   ```bash
   curl -i http://localhost:8001/users/2/profile      # HTTP 500
   ```
   La demo envía el evento a `POST /api/v1/ingest/events` con su clave de ingesta.
2. **Ver el incidente.** En el dashboard (`http://localhost:5173`) aparece en **Incidents** un incidente `AttributeError` de `demo-app`, con severidad, contador y timeline (recibido, agrupado). Repetir la petición incrementa el contador del mismo incidente (mismo fingerprint).
   Vía API: `curl -H "Authorization: Bearer $SENTINEL_ADMIN_TOKEN" http://localhost:8000/api/v1/incidents`.
3. **Pedir el RCA.** En el detalle del incidente, botón de RCA, o `POST /api/v1/incidents/{id}/rca`. El worker ejecuta el motor RCA: con el proveedor mock se obtiene un informe determinista (marcado como mock) con hipótesis, evidencia, confianza, archivos/funciones sospechosos (`demo_app/main.py`, `_display_name`), incertidumbres y próximos pasos. Consultar con `GET /api/v1/incidents/{id}/rca`.
4. **Pedir la reparación.** Botón de reparación o `POST /api/v1/incidents/{id}/repair`. El motor crea una copia aislada de `demo-app/` en `SENTINEL_WORKDIR`, una rama `sentinel/fix-<incidente>-<n>`, aplica un parche validado con un test de regresión y ejecuta `python -m pytest -q -m "not fault_reproduction"`. Solo si los tests pasan el intento es exitoso y la hipótesis del RCA pasa a "confirmada". El detalle muestra el diff, la salida de los tests, los intentos y el costo (`GET /api/v1/incidents/{id}/repairs`).
5. **Preparar el PR en borrador.** Botón de PR o `POST /api/v1/incidents/{id}/pull-request`. Con `SENTINEL_GITHUB_PUBLISH=false` (por defecto) el PR queda **preparado** (título y cuerpo con el RCA, el diff y los resultados de los tests) sin publicarse. Se rechaza (409) si la reparación no fue verificada por tests.
6. **Historial y costos.** La vista **Runs** muestra cada ejecución de RCA y reparación con duración y resultado, y los costos de IA (“no disponible” en modo mock).

Este mismo flujo está cubierto de punta a punta, con fakes y sin red, por `backend/tests/test_e2e_flow.py`.

## Referencia de variables de entorno

Toda la configuración del backend proviene de variables de entorno con prefijo `SENTINEL_` (clase `Settings` en `backend/app/config.py`). No se lee ningún archivo `.env`. Todos los valores tienen un valor por defecto seguro.

| Variable | Por defecto | Descripción |
|---|---|---|
| `SENTINEL_DATABASE_URL` | `sqlite:///./sentinel.db` | URL de SQLAlchemy de la base de datos. |
| `SENTINEL_ADMIN_TOKEN` | (sin definir) | Token Bearer de la API de administración y del dashboard. Sin él esas rutas responden 503. |
| `SENTINEL_AI_PROVIDER` | `mock` | Proveedor de IA: `mock`, `anthropic` (`openai`, `gemini`: puntos de extensión no implementados). |
| `SENTINEL_ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Modelo usado por el adaptador Anthropic. |
| `SENTINEL_ANTHROPIC_MAX_TOKENS` | `4096` | Máximo de tokens de salida por llamada a Anthropic. |
| `SENTINEL_ANTHROPIC_INPUT_USD_PER_MTOK` | `3.0` | Precio estimado (USD por millón de tokens de entrada) para calcular costos. |
| `SENTINEL_ANTHROPIC_OUTPUT_USD_PER_MTOK` | `15.0` | Precio estimado (USD por millón de tokens de salida). |
| `SENTINEL_GITHUB_PUBLISH` | `false` | Si es `true` (y el repositorio tiene `github_repo`), hace push de la rama de corrección y crea el PR en borrador con `gh`. Por defecto solo se prepara. |
| `SENTINEL_WORKDIR` | `./var/workdir` | Directorio donde se crean las copias aisladas para reparar. |
| `SENTINEL_MAX_REPAIR_ATTEMPTS` | `3` | Intentos máximos de parche por reparación. |
| `SENTINEL_REPAIR_TIME_BUDGET_SECONDS` | `1800` | Presupuesto de tiempo total de una reparación. |
| `SENTINEL_REPAIR_TEST_TIMEOUT_SECONDS` | `300.0` | Timeout de cada ejecución del comando de tests. |
| `SENTINEL_JOB_TIMEOUT_SECONDS` | `600` | Tiempo máximo de un job antes de marcarse como fallido por timeout. |
| `SENTINEL_MAX_PATCH_FILES` | `20` | Archivos máximos que puede tocar un parche. |
| `SENTINEL_MAX_PATCH_BYTES` | `204800` | Tamaño máximo de un parche (bytes). |
| `SENTINEL_RUN_JOBS_INLINE` | `false` | Procesa los jobs dentro de la petición que los encola (sin worker). |
| `SENTINEL_JOB_MAX_ATTEMPTS` | `3` | Reintentos máximos de un job RCA fallido. |
| `SENTINEL_JOB_RETRY_BACKOFF_SECONDS` | `30` | Espera antes de reintentar un job. |
| `SENTINEL_WORKER_POLL_SECONDS` | `2.0` | Intervalo de sondeo del worker. |
| `SENTINEL_MAX_BODY_BYTES` | `262144` | Tamaño máximo del cuerpo de ingesta (413 si se excede). |
| `SENTINEL_MAX_MESSAGE_LENGTH` | `4096` | Longitud máxima del mensaje de la excepción. |
| `SENTINEL_MAX_STACK_TRACE_LENGTH` | `65536` | Longitud máxima del stack trace. |
| `SENTINEL_MAX_METADATA_ITEMS` | `50` | Cantidad máxima de claves de metadatos. |
| `SENTINEL_MAX_METADATA_BYTES` | `8192` | Tamaño máximo de los metadatos serializados. |

Variables fuera del prefijo, leídas solo en el servidor:

- `ANTHROPIC_API_KEY`: credencial del proveedor Anthropic.
- `GH_TOKEN` / `GITHUB_TOKEN`: credenciales que se reenvían a `gh` cuando la publicación está activa.

`backend/tests/test_docs.py` verifica que cada campo de `Settings` esté documentado en esta tabla.

## Proveedores de IA

- **`mock` (por defecto):** determinista, sin red ni credenciales. Conoce las tres fallas de la demo y produce RCAs y parches reproducibles. Todo lo que genera se etiqueta como mock y los costos se muestran como “no disponible”.
- **`anthropic`:** se activa con `SENTINEL_AI_PROVIDER=anthropic` y `ANTHROPIC_API_KEY` definida **en el servidor** (nunca en el frontend ni en el repositorio). Usa `SENTINEL_ANTHROPIC_MODEL` y reporta tokens y costo estimado en USD.
- **`openai` / `gemini`:** puntos de extensión documentados en `backend/app/ai/registry.py`. Aún no están implementados y responden “not configured”. Para agregar uno, implementar un adaptador `AIProvider` (`backend/app/ai/base.py`) y construirlo en el registro cuando existan sus credenciales.

`GET /api/v1/health` informa el proveedor seleccionado y cuáles están configurados.

> **Nota:** una suscripción a Claude Code Enterprise (o a claude.ai) **no** incluye una clave de API. Para usar el proveedor `anthropic` hace falta una clave de la API de Anthropic (Claude Console) con facturación propia.

## Reglas de severidad

La severidad es determinista (`backend/app/ingest/severity.py`). Niveles: `low` < `medium` < `high` < `critical`.

1. **Nivel base por tipo de excepción** (se usa el último componente del nombre, p. ej. `sqlalchemy.exc.OperationalError` → `OperationalError`):
   - `critical`: `MemoryError` y errores de conexión a la base de datos (`OperationalError`, `InterfaceError`, `DatabaseError`, `DatabaseConnectionError`, `DBConnectionError`).
   - `high`: excepciones no manejadas que terminan en HTTP 5xx (`TypeError`, `AttributeError`, `KeyError`, `IndexError`, `LookupError`, `NameError`, `UnboundLocalError`, `ZeroDivisionError`, `RuntimeError`, `RecursionError`, `AssertionError`, `NotImplementedError`).
   - `medium`: errores de entrada (`ValidationError`, `RequestValidationError`, `ValueError`, `UnicodeDecodeError`).
   - `low`: cualquier otra.
2. **+1 nivel** si el incidente recibió algún evento del entorno `production` (o `prod`).
3. **+1 nivel** por cada umbral de frecuencia alcanzado: 10 y 100 eventos.
4. El resultado se limita a `critical`.

Un incidente resuelto que vuelve a ocurrir se reabre como regresión.

## Modelo de seguridad

- **Ingesta autenticada por aplicación:** cada aplicación tiene su propia clave (`X-Sentinel-Key`), guardada solo como hash SHA-256 y comparada en tiempo constante. Una clave nunca permite escribir en otra aplicación.
- **API de administración** protegida por `SENTINEL_ADMIN_TOKEN` (Bearer); desactivada (503) si no está configurado.
- **Validación y límites:** Pydantic más límites de cuerpo, mensaje, stack trace y metadatos (413/422).
- **Redacción de secretos** antes de persistir y en todas las salidas (API, prompts, informes, salida de tests, cuerpos de PR): tokens Bearer, claves de API, contraseñas en URLs y pares `password=`, `secret=`, `token=`, `api_key=`, etc.
- **Prompts resistentes a inyección:** el contenido de eventos y del repositorio se trata como datos no confiables, delimitado y nunca como instrucciones; las respuestas del modelo se validan con Pydantic.
- **Repositorios autorizados:** solo los registrados por un administrador, con comando de tests fijo. Las herramientas de lectura impiden path traversal, symlinks fuera del repositorio y archivos demasiado grandes.
- **Reparaciones aisladas:** siempre en una copia en `SENTINEL_WORKDIR` y en una rama nueva; los parches no pueden tocar `.git`, configuración de CI ni secretos, y tienen límites de tamaño.
- **Ejecución de comandos:** argv sin shell, timeout, entorno mínimo, salida truncada y redactada; allow-list de subcomandos `git`/`gh` (nunca merge, nunca push a la rama base ni a repositorios no autorizados).
- **Verificación obligatoria:** una reparación solo es exitosa si los tests pasan después del parche; la afirmación del modelo nunca alcanza. Nunca se crea un PR si fallan los tests.
- **Publicación desactivada por defecto** (`SENTINEL_GITHUB_PUBLISH=false`); los PR son siempre en borrador.
- **Sin secretos en el repositorio:** sin archivos `.env`, claves ni CI (`backend/tests/test_repo_hygiene.py` lo verifica).

## Docker (solo documentación)

`docker-compose.yml` en la raíz muestra cómo empaquetar backend, worker, frontend y demo. **No se construye ni se verifica**; es una preparación a nivel de documentación. Espera un `Dockerfile` multi-stage en la raíz (todavía no incluido) con los targets `backend` (python:3.12-slim + `git`, `pip install -r backend/requirements.txt`, copia de `backend/` y `demo-app/`, usuario sin privilegios, `uvicorn app.main:app --host 0.0.0.0 --port 8000`), `demo-app` (`uvicorn demo_app.main:create_app --factory --port 8001`) y `frontend` (`npm ci && npm run build`, servido por nginx con un proxy de `/api` a `http://api:8000`). Las credenciales se pasan por variables de entorno del host (nunca se copian a la imagen).

```bash
export SENTINEL_ADMIN_TOKEN="un-token-local-largo"
docker compose up --build
docker compose run --rm api python -m app.seed   # imprime la clave de ingesta de demo-app
```

## Próximos pasos

- **Aislamiento con contenedores:** ejecutar los tests de cada reparación en un contenedor efímero sin red, con CPU/memoria limitadas y sistema de archivos de solo lectura salvo la copia de trabajo.
- **MCP:** exponer las herramientas de lectura del repositorio y los informes RCA como servidor MCP, y consumir fuentes de contexto externas (logs, tickets) mediante MCP.
- **OpenTelemetry:** ingerir trazas y logs OTel (OTLP) además de eventos propios, para correlacionar excepciones con spans.
- Implementar los adaptadores `openai` y `gemini`.
- Base de datos de producción (PostgreSQL) y migraciones.
