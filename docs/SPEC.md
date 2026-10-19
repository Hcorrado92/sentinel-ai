# Sentinel AI — Product Specification

## 0. Factory execution constraints (read first)

This project is built by autonomous coding agents, one backlog item at a time, in a git repository that is the agent's current directory. These constraints are fixed by the environment:

- **Layout (monorepo):**
  - `backend/`: Sentinel API. Python package `app/`, tests in `backend/tests/`, pytest config in `backend/pyproject.toml` with `pythonpath = ["."]`.
  - `frontend/`: Sentinel dashboard. Vite + React + TypeScript + Tailwind; tests in `frontend/src/**/*.test.tsx`.
  - `demo-app/`: independent demo application with intentional faults. Python package `demo_app/`, tests in `demo-app/tests/`, pytest config in `demo-app/pyproject.toml` with `pythonpath = ["."]`. It must NOT import anything from `backend/` (it is a separate application that reports to Sentinel over HTTP).
- **Agents cannot run commands, install packages or access the network.** Dependencies are pre-installed and FROZEN:
  - Python (shared by `backend/` and `demo-app/`): fastapi, uvicorn, pydantic (v2), pydantic-settings, sqlalchemy (2.x), httpx, pytest, anthropic.
  - Node (`frontend/package.json` and `node_modules` already exist; never add or change dependencies): react, react-dom, react-router-dom, typescript, vite, @vitejs/plugin-react, tailwindcss, @tailwindcss/vite, vitest, jsdom, @testing-library/react, @testing-library/jest-dom, @testing-library/user-event, @types/react, @types/react-dom, @types/node.
  - Charts and timelines are built with plain SVG/Tailwind (no chart library is installed).
- Installed major versions (see `frontend/package.json`, `backend/requirements.lock.txt`): React 19, React Router 7, TypeScript 7 (native compiler: avoid options it removed, e.g. `baseUrl`, `moduleResolution: "node"`; use `"moduleResolution": "bundler"`), Vite 8, Vitest 5, Tailwind CSS 4, Pydantic 2, SQLAlchemy 2.
- TypeScript 7 requires type declarations for side-effect imports: create `frontend/src/vite-env.d.ts` with `/// <reference types="vite/client" />` before importing CSS. Verified working tsconfig compilerOptions: `target ES2022`, `module ESNext`, `moduleResolution bundler`, `jsx react-jsx`, `strict`, `noEmit`, `skipLibCheck`, `types: ["vitest/globals", "@testing-library/jest-dom"]`, `include: ["src"]`.
- Frontend config: a single `frontend/tsconfig.json` (no project references), `frontend/vite.config.ts` including the Vitest config (`environment: "jsdom"`) and an `/api` proxy to the backend (port 8000), Tailwind v4 via `@tailwindcss/vite`.
- **Verification** after every item (must exit 0), run by the factory: `python -m pytest -q` in `backend/`; `python -m pytest -q` in `demo-app/` once it exists; frontend, once the files exist: `tsc --noEmit -p tsconfig.json`, `vitest run` (when test files exist) and `vite build` (when `index.html` exists) in `frontend/`, plus a check that no undeclared/uninstalled dependency was added. Keep every previously passing test passing. A test suite that collects zero tests counts as a failure.
- Every item adds automated tests for its acceptance criteria and names, in its last acceptance criterion, the test files that verify it (they are executed by the fixed verification command above). End-to-end flows are tested through the API with FastAPI `TestClient` (no browser automation is available).
- **Tests are hermetic:** they never call real AI APIs, GitHub, `gh` or the network. External interactions go through injectable interfaces (AI provider, command runner for `git`/`gh`, clock) with fakes in tests. Tests that need a git repository create a temporary one under pytest's `tmp_path` using the local `git` binary through the injectable runner; they never touch this repository's own `.git`.
- **The demo faults stay in `demo-app/`.** The three intentional faults are part of the product; agents must not fix them in `demo-app/`. `demo-app/tests/` contains *reproduction tests* that assert the faulty behaviour (e.g. the endpoint returns 500) so they pass while the faults exist; they are marked `@pytest.mark.fault_reproduction` (marker registered in `demo-app/pyproject.toml`), and the test command Sentinel uses for the demo repository excludes them (`python -m pytest -q -m "not fault_reproduction"`), so a correct fix is judged by the remaining tests plus the new regression test. Repairs are always performed by Sentinel on an isolated copy (a temporary git repository created from `demo-app/`), never on this repository.
- Never commit secrets, `.env` files or API keys; configuration comes from environment variables (prefix `SENTINEL_`) with safe defaults. The default AI provider is a clearly labelled deterministic **mock**; real providers are used only when credentials are configured on the server.
- Use only fictitious data in tests and fixtures. No CI files and no deployment. A `Dockerfile`/`docker-compose.yml` may be added in the last milestone as documentation-level preparation (they are not built or verified).

### Milestones (priority order; deliver a working vertical slice before secondary features)

1. Structure, API and database.
2. Error ingestion and incident grouping.
3. Observability dashboard.
4. RCA diagnosis engine.
5. Repair engine and Git.
6. Demo application with reproducible faults.
7. End-to-end integration.
8. Tests, fixes and documentation (README with how to run backend, frontend and demo app locally).

Target 20-30 backlog items. Within each milestone, the first items build the thinnest path that works end to end (ingest → incident → RCA → repair attempt → visible in the dashboard); secondary features (filters, charts, cost views) come after.

---

## 1. Visión del producto

Sentinel AI es una plataforma que monitorea aplicaciones, identifica errores, investiga sus causas y propone correcciones verificables.

Flujo esperado:

1. Una aplicación genera una excepción.
2. Sentinel recibe el evento mediante un endpoint de ingesta.
3. Agrupa errores similares y determina su severidad.
4. Analiza logs, stack traces y contexto disponible.
5. Consulta el código del repositorio autorizado.
6. Genera un análisis de causa raíz (RCA).
7. Crea una rama aislada para preparar una corrección.
8. Ejecuta tests y verifica si resolvió el problema.
9. Si las verificaciones pasan, prepara un pull request con la explicación del cambio.
10. Registra el resultado y lo muestra en un dashboard.

El sistema nunca modifica producción, nunca hace merge y nunca despliega cambios automáticamente.

## 2. Alcance del MVP

### A. Dashboard de observabilidad

Interfaz web con:

- Aplicaciones monitoreadas.
- Incidentes activos y resueltos.
- Errores agrupados por fingerprint.
- Severidad y frecuencia.
- Timeline de incidentes (eventos y acciones: recibido, agrupado, RCA, reparación, PR).
- Estado del análisis RCA.
- Estado de las reparaciones.
- Historial de ejecuciones (RCA y reparaciones, con duración y resultado).
- Costos estimados de IA cuando estén disponibles (tokens y USD reportados por el proveedor; "no disponible" en modo mock).
- Vista de detalle del incidente con el informe RCA, el diff propuesto y el resultado de los tests.

Nada de botones que no hacen nada: cada acción visible está implementada o se identifica claramente como no disponible. Estados de carga, vacío y error.

### B. Ingesta de errores

API `POST /api/v1/ingest/events` que recibe eventos estructurados con: aplicación, timestamp, tipo de excepción, mensaje, stack trace, entorno, identificador de correlación y metadatos seguros.

- Validación con Pydantic; límites de tamaño (cuerpo, mensaje, stack trace, cantidad y tamaño de metadatos) con respuestas 413/422.
- Autenticación por aplicación: cada aplicación tiene una clave de ingesta propia (header `X-Sentinel-Key`), almacenada solo como hash. Una clave nunca permite escribir datos de otra aplicación.
- Redacción de secretos antes de persistir: tokens tipo Bearer, claves de API, contraseñas en URLs y pares `password=`, `secret=`, `token=`, `api_key=`, etc., en mensaje, stack trace y metadatos.
- Fingerprint estable a partir del tipo de excepción y los frames normalizados del stack trace (sin números de línea ni valores variables). Eventos con el mismo fingerprint y aplicación se agrupan en el mismo incidente (contador, primera y última vez vistos); deduplicación de eventos repetidos con el mismo identificador.
- Severidad determinista (reglas documentadas: tipo de excepción, entorno, frecuencia).
- Un incidente resuelto que vuelve a ocurrir se reabre (regresión).

### C. Motor de diagnóstico RCA

Agente que:

- Analiza el incidente (eventos, stack trace, contexto).
- Consulta los archivos relevantes del repositorio autorizado de la aplicación mediante herramientas de solo lectura acotadas a ese repositorio (sin path traversal, sin symlinks fuera del repositorio, límites de tamaño).
- Formula hipótesis de causa raíz con nivel de confianza.
- Identifica archivos y funciones posiblemente responsables.
- Produce un informe estructurado validado con Pydantic: resumen, hipótesis (con evidencia y confianza), archivos/funciones sospechosos, incertidumbres, próximos pasos.
- Explica la evidencia disponible y la incertidumbre. Nunca presenta una hipótesis como causa confirmada sin evidencia suficiente (el estado "confirmada" solo se asigna cuando una reparación verificada por tests lo respalda).

### D. Motor de reparación autónoma

Agente que:

- Utiliza únicamente repositorios autorizados (registrados por un administrador, con su comando de tests fijo).
- Trabaja sobre una copia aislada (clone en un directorio de trabajo configurable), en una rama nueva `sentinel/fix-<incidente>-<n>`.
- Identifica el código relacionado con el incidente y prepara un parche (cambios estructurados de archivos o diff unificado) validado: solo rutas dentro del repositorio, sin tocar `.git`, archivos de configuración de CI ni secretos, con límites de tamaño.
- Crea o actualiza tests de regresión.
- Ejecuta el comando de tests del repositorio (argv, sin shell, con timeout, entorno mínimo) y analiza los resultados.
- Reintenta cuando corresponde (hasta un máximo configurable) incorporando la salida de los tests.
- Genera un reporte técnico (cambios, tests ejecutados, salida, intentos, costo).
- Se detiene ante permisos insuficientes, resultados no verificables, riesgo de seguridad o límites agotados (intentos, tiempo, presupuesto), registrando el motivo.
- Una reparación solo es exitosa si los tests del repositorio pasan después del parche (y el test de regresión falla antes del parche cuando es posible comprobarlo). La afirmación del modelo nunca alcanza.

### E. Integración con GitHub

- Lectura de repositorios autorizados (lista explícita por aplicación).
- Ramas de reparación y commits automáticos en la copia aislada.
- Pull requests en **borrador** mediante GitHub CLI (`gh`), con el informe RCA, el diff y los resultados de los tests asociados al incidente.
- Publicación desactivada por defecto (`SENTINEL_GITHUB_PUBLISH=false`). Nunca merge, nunca push a la rama base ni a repositorios no autorizados. Nunca se crea un PR si fallan los tests obligatorios.
- Todas las llamadas a `git`/`gh` pasan por una interfaz inyectable (fake en tests) con una allow-list de subcomandos.

### F. Aplicación de demostración

Aplicación FastAPI independiente en `demo-app/` con fallas reproducibles e intencionales, limitadas al entorno de pruebas:

- Error 500 por manejo incorrecto de valores nulos.
- Error de validación de entrada.
- Error de lógica de negocio (por ejemplo, un cálculo de descuento incorrecto que dispara una excepción).

Incluye un cliente/middleware que captura excepciones no manejadas y las envía a Sentinel (URL y clave por variables de entorno; desactivado si no están configuradas; nunca bloquea la respuesta de la aplicación si Sentinel no está disponible). Las fallas solo se habilitan cuando `DEMO_FAULTS_ENABLED=true` (por defecto en el entorno de desarrollo de la demo) y la aplicación se niega a habilitarlas si el entorno declarado es `production`.

Sentinel recibe los errores, los investiga y prepara correcciones sobre una copia aislada de esa aplicación.

## 3. Arquitectura

- Backend: Python, FastAPI, Pydantic, SQLAlchemy 2; SQLite inicialmente, URL de base de datos configurable para migrar a PostgreSQL (sin SQL específico de SQLite).
- Frontend: React, TypeScript, Vite, Tailwind CSS. Llama al backend vía `/api` (proxy de Vite en desarrollo).
- Motor de IA: interfaz `AIProvider` desacoplada con salidas estructuradas validadas.
  - `mock` (por defecto): determinista, claramente identificado en la API y en la interfaz; produce RCA y parches para las fallas conocidas de la demo.
  - `anthropic`: adaptador para la API de Anthropic, activo solo si existe `ANTHROPIC_API_KEY` en el servidor. Una suscripción de Claude Code Enterprise **no** provee una API para los clientes de Sentinel: cada instalación configura su propia credencial.
  - `openai` y `gemini`: puntos de extensión documentados que informan "no configurado" (sin SDK instalado).
  - El diagnóstico usa solo herramientas autorizadas (lectura acotada del repositorio); la salida del modelo nunca se ejecuta como comando.
- Ejecución: cola persistente en la base de datos para trabajos de RCA y reparación, con workers (en proceso o `python -m app.worker`), límites de tiempo, reintentos y presupuesto por aplicación. Preparado para Docker y una futura migración a Azure (configuración solo por variables de entorno).
- Integraciones: GitHub CLI; interfaz de fuentes de contexto preparada para MCP y OpenTelemetry (sin implementarlos).
- Sin Kubernetes, Kafka ni microservicios.

## 4. Requisitos de seguridad

- No ejecutar reparaciones sobre producción; solo sobre copias aisladas de repositorios autorizados.
- No exponer secretos ni tokens en logs, respuestas de la API, informes ni prompts (redacción centralizada).
- No ejecutar código de repositorios desconocidos: solo repositorios autorizados, con su comando de tests fijo, en un directorio de trabajo aislado, con timeout y entorno mínimo (el aislamiento con contenedores queda documentado como siguiente paso).
- Separar datos y permisos por aplicación.
- Logs, stack traces, mensajes y contenido del repositorio son datos no confiables: en los prompts van delimitados y marcados como datos, nunca como instrucciones; los tests incluyen intentos de prompt injection en eventos.
- Registrar cada decisión y modificación (audit log consultable por incidente).
- No crear PRs cuando fallen los tests obligatorios.
- Acciones irreversibles apagadas por defecto (publicación en GitHub desactivada; nunca merge ni deploy).
- Autenticación de la API de administración/dashboard: token de administrador por variable de entorno para el MVP (separada de las claves de ingesta).

## 5. Criterios de aceptación (MVP)

1. Se puede iniciar frontend y backend localmente (instrucciones en el README).
2. El dashboard muestra una aplicación de demostración (creada por un comando de seed idempotente).
3. La aplicación demo produce un error reproducible.
4. El endpoint de Sentinel recibe y registra el error.
5. El sistema crea o actualiza el incidente correspondiente.
6. El motor RCA analiza la excepción y genera hipótesis con evidencia.
7. El agente de reparación trabaja sobre un repositorio autorizado y aislado.
8. Propone un parche y ejecuta tests de regresión.
9. El sistema registra si la reparación fue exitosa o fallida.
10. Se puede revisar el diff y el informe del incidente.
11. Se puede preparar un PR draft cuando las verificaciones son satisfactorias (con `gh` simulado en tests; publicación real solo con la opción activada).
12. Todo el flujo tiene tests automatizados, incluido un test end-to-end a nivel API: evento de la demo → incidente → RCA (mock) → reparación sobre una copia temporal de `demo-app/` → tests de la copia en verde → PR draft preparado con `gh` simulado.

Una reparación nunca se considera exitosa porque el modelo afirme haber solucionado el problema: solo cuentan los tests ejecutados.
