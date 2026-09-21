# Pla d'implementació — prototip de bot de coneixement per Telegram

**Objectiu:** implementar un prototip funcional, en Python i amb cost obligatori de **0 €**, que:
1. reculli tots els missatges d'un grup privat de Telegram de test;
2. utilitzi com a base inicial de coneixement el Q&A existent a `https://bhc-fcb.netlify.app/` i un export/extracte del grup de WhatsApp;
3. detecti preguntes;
4. recuperi Q&A i missatges rellevants;
5. respongui de manera fundada i amb referències;
6. permeti marcar una resposta com a incorrecta;
7. gestioni **tot el flux de correcció i aprovació dins de Telegram**;
8. mantingui una base de Q&A versionada preparada perquè una web externa la consumeixi en una fase 2;
9. deixi el backend desacoblat del canal, de manera que més endavant es puguin afegir Email, WhatsApp, Slack o altres adaptadors sense tocar el core.

Aquest document està escrit com a **spec d'implementació per a un agent de coding tipus DeepSeek**. Cal seguir les decisions d'arquitectura indicades i evitar introduir components nous si no són necessaris.

---

## 0. Decisions tancades

### Stack de producció del prototip

- **Llenguatge:** Python.
- **Runtime:** Cloudflare Python Workers.
- **HTTP runtime:** FastAPI sobre l'ASGI runtime oficial de Cloudflare Python Workers (`workers.asgi.entrypoint(app)`).
- **Persistència:** Cloudflare D1.
- **Vector search:** Cloudflare Vectorize.
- **Embeddings + classificador semàntic:** `@cf/google/embeddinggemma-300m`.
- **Model generatiu:** `@cf/zai-org/glm-4.7-flash`.
- **Canal runtime v1:** Telegram Bot API.
- **Imports offline inicials:**
  - snapshot del Q&A de BHC/FCB;
  - export de WhatsApp.
- **Models i validació runtime:** Pydantic v2.
- **CLI d'operacions i imports:** Typer.
- **Logging:** Loguru, amb logs estructurats a stdout i sense PII/text de missatges per defecte.
- **Arquitectura:** Clean Architecture pragmàtica amb dependency rule explícita.
- **Entorn de desenvolupament i CI:** Docker (imatge pròpia, sense Docker Compose).
- **Tests:** `pytest`.
- **Evals:** `pydantic-evals` per als datasets/evaluadors offline quan aporti valor, més checks deterministes propis.
- **AI orchestration:** NO fer de PydanticAI una dependència obligatòria de la v1. El core ha de dependre d'un port `Generator`; es pot afegir un adapter PydanticAI només si un smoke test confirma compatibilitat amb Cloudflare Python Workers/Pyodide. Per defecte, usar el binding natiu de Workers AI + Pydantic per validar output.
- **Gestió de dependències:** `uv`.
- **Lint + format:** Ruff.
- **Type checking:** `ty`.
- **Deploy:** `pywrangler`.
- **Producció:** Cloudflare Python Worker, no un contenidor. Docker no ha d'introduir cost de producció.
- **Web pròpia:** **NO implementar en fase 1**.
- **Processament d'imatges:** **NO implementar en fase 1**.
- **Cost:** el sistema **no pot tenir cap fallback de pagament**.

### Per què Cloudflare

A data 2026-09-19:

- Python Workers és una funcionalitat de primera classe i pot accedir a D1, Workers AI, Vectorize, KV, R2, etc.
- Workers Free: 100.000 requests/dia, 10 ms de CPU activa per HTTP request, 128 MB RAM.
- D1 Free: 5 M files llegides/dia, 100.000 files escrites/dia, 500 MB per DB.
- Vectorize Free: 30 M dimensions consultades/mes i 5 M dimensions emmagatzemades.
- Workers AI Free: 10.000 neurons/dia.
- `@cf/zai-org/glm-4.7-flash` continua disponible al Workers Free plan.
- `EmbeddingGemma-300M` té 768 dimensions i està pensat per cerca, similitud, clustering i classificació multilingüe.

**Regla de cost:** no activar Workers Paid ni configurar models que requereixin billing. Si s'arriba a un límit gratuït, el sistema ha de degradar de manera segura i no fer cap crida de pagament.

---

# 1. Abast de la v1

## Inclòs

- Un grup privat de Telegram creat manualment per l'usuari.
- Un bot de Telegram afegit al grup.
- Recepció de tots els missatges textuals.
- Persistència dels missatges.
- Persistència de metadades d'imatges, sense descarregar-les.
- Import de la base Q&A existent.
- Import d'un export de WhatsApp.
- Embeddings de Q&A i missatges.
- Detecció de preguntes (per a indexació i ranking).
- Resposta a preguntes **només quan el bot és adreçat explícitament** (menció, resposta
al bot, missatge directe o `/ask`). Una pregunta sense adreçar no genera resposta.
- Resum periòdic configurable de preguntes i respostes (vegeu §26.1).
- Recuperació semàntica.
- Respostes RAG compactes.
- Referències a les fonts.
- Abstenció quan no hi ha evidència suficient.
- Botó `⚠️ Està malament?`.
- Proposta de correcció via Telegram.
- Revisió privada per l'admin.
- Aprovar / rebutjar / editar correccions.
- Versionat dels Q&A.
- Les correccions aprovades passen a ser la font de màxima autoritat.
- Evals offline.
- Evals live opcionals.
- Integration tests del flow de pregunta.
- Integration tests del flow complet de correcció.
- Test explícit que una imatge no s'intenta processar.

## No inclòs

- Web pública pròpia.
- Modificació del web `bhc-fcb.netlify.app`.
- OAuth.
- Panell d'administració web.
- Processament d'imatges.
- OCR.
- Transcripció d'àudios.
- WhatsApp runtime.
- Email runtime.
- Fine-tuning.
- Microserveis.
- Kubernetes / Docker / VM.
- Redis.
- Postgres.
- LangChain.

---

# 2. Restricció important: el grup de Telegram

El Bot API no crea el grup per nosaltres. Per al prototip, l'usuari farà manualment:

1. Crear un grup privat de Telegram, per exemple:
   - `BHC Bot Test`
2. Crear el bot amb `@BotFather`.
3. Afegir el bot al grup.
4. Fer que el bot pugui veure els missatges:
   - preferiblement desactivar Privacy Mode amb `/setprivacy`, o
   - fer-lo administrador.
5. Enviar `/chatid` al grup.
6. El bot ha de respondre el `chat_id`.
7. Configurar aquest valor com a entrada de `ALLOWED_TELEGRAM_CHAT_IDS`.
8. En privat amb el bot, enviar `/whoami`.
9. Configurar el `user_id` retornat com `ADMIN_TELEGRAM_USER_ID`.

Durant el prototip només s'acceptaran updates del grup configurat.

---

# 3. Principi d'arquitectura

El **core no pot dependre de Telegram**.

Telegram és només un adaptador.

```text
                      INBOUND SOURCES

          +-----------+----------+-----------+
          |                      |           |
       Telegram              WhatsApp      Web Q&A
       runtime                import        snapshot
          |                      |           |
          +-----------+----------+-----------+
                      |
                      v
              NormalizedMessage
                      |
                      v
            +--------------------+
            |   CORE BACKEND     |
            |                    |
            | ingest             |
            | classify           |
            | index              |
            | retrieve           |
            | answer             |
            | knowledge update   |
            | feedback           |
            | moderation         |
            +---------+----------+
                      |
              +-------+-------+
              |               |
              v               v
             D1           Vectorize
              |
              v
         versioned Q&A
              |
              v
           OUTBOUND
              |
          Telegram
```

En una fase futura:

```text
EmailAdapter    \
WhatsAppAdapter  \
SlackAdapter      ---> mateix core
TelegramAdapter  /
```

No duplicar lògica de negoci dins dels adapters.

---

# 3.1. Toolchain Python obligatori

L'agent ha d'utilitzar aquest toolchain i no substituir-lo per alternatives equivalents sense una raó documentada:

```text
uv              dependències, entorn i execució
Docker          entorn reproduïble de desenvolupament/CI (sense Compose)
FastAPI         HTTP/API
Pydantic v2     DTOs, settings i validació de fronteres
Typer           totes les CLIs de manteniment/import/evals
Loguru          logging estructurat
Ruff            lint + format
ty              type checking
pytest          tests
pydantic-evals  datasets/evals offline quan sigui útil
pywrangler      runtime local fidel + deploy Cloudflare Python Workers
```

Ordres de qualitat obligatòries:

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
```

Comanda local per aplicar format:

```bash
uv run ruff format .
uv run ruff check --fix .
```

No afegir Black, isort, Flake8, mypy o Pyright.

La via preferida per a desenvolupament ha de funcionar tant directament amb `uv` com dins Docker.
No s'utilitza Docker Compose; totes les comandes de contenidor són `docker build` i `docker run` directes:

```bash
docker build -t knowledge-bot:dev .

docker run --rm -v "$PWD":/workspace -w /workspace knowledge-bot:dev \
  uv run ruff format --check .
docker run --rm -v "$PWD":/workspace -w /workspace knowledge-bot:dev \
  uv run ruff check .
docker run --rm -v "$PWD":/workspace -w /workspace knowledge-bot:dev \
  uv run ty check
docker run --rm -v "$PWD":/workspace -w /workspace knowledge-bot:dev \
  uv run pytest
```

Per aixecar el Worker local:

```bash
docker run --rm -it -p 8787:8787 -v "$PWD":/workspace -w /workspace \
  knowledge-bot:dev uv run pywrangler dev --local --ip 0.0.0.0 --port 8787
```

El contenidor ha d'exposar el port `8787`.

No usar Docker-in-Docker. No usar Cloudflare Containers en producció per aquesta v1.
**No s'utilitza Docker Compose**: no hi ha `compose.yaml` i no s'ha d'introduir.

## PydanticAI

PydanticAI **no és necessari per al flux principal de la v1**.

Raons:

1. el flux no és agentic;
2. retrieval i policies són deterministes;
3. Workers AI es pot invocar directament;
4. Pydantic ja ens dona validació d'outputs;
5. volem minimitzar dependències runtime dins Pyodide.

Definir, però, aquesta frontera:

```python
class AnswerGenerator(Protocol):
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        ...
```

Implementació obligatòria v1:

```text
WorkersAIGenerator
```

Possible implementació futura:

```text
PydanticAIGenerator
```

Només incorporar `pydantic-ai` al runtime si passa un smoke test real dins `pywrangler dev` i desplegat a Cloudflare. No assumir compatibilitat perquè funcioni en CPython local.

Per a la suite d'evals sí que es pot usar `pydantic-evals` en entorn de desenvolupament, sense empaquetar-lo al Worker.

---

# 3.2. CLI única amb Typer

Crear:

```text
src/cli.py
```

amb una app Typer i subcomandes.

Interfície prevista:

```bash
uv run kb snapshot-web --url https://bhc-fcb.netlify.app/ --out data/seed/bhc_fcb_qa.json

uv run kb import-whatsapp \
  data/raw/whatsapp.txt \
  --out data/seed/whatsapp_messages.jsonl

uv run kb seed
uv run kb reindex
uv run kb set-telegram-webhook
uv run kb delete-telegram-webhook

uv run kb eval offline
uv run kb eval live
```

Els scripts ad-hoc no han de contenir lògica pròpia. Si es mantenen fitxers sota `scripts/`, han de ser wrappers mínims o eliminar-se a favor de la CLI Typer.

La CLI reutilitza exactament els mateixos serveis d'aplicació que el backend; no duplicar parsers o lògica.

---

# 3.3. Clean Architecture: regla obligatòria

La separació ha de ser arquitectònica, no només una distribució de carpetes.

Dependency rule:

```text
domain
  ↑
application
  ↑
ports / contracts
  ↑
adapters + infrastructure
  ↑
FastAPI / Telegram / Cloudflare / Typer
```

En termes pràctics:

```text
DOMAIN
- entitats
- value objects
- enums
- invariants
- decisions que no necessiten I/O

APPLICATION
- use cases
- orchestration
- policies
- transaccions lògiques
- depèn només del domain + ports

PORTS
- Protocols/interfaces
- repositories
- vector search
- AI
- transport
- clock
- media processor

ADAPTERS / INFRASTRUCTURE
- Telegram Bot API
- FastAPI routes
- D1
- Vectorize
- Workers AI
- Web snapshot parser
- WhatsApp export parser
- Loguru configuration
```

## Regles estrictes

`domain/` i `application/`:

```text
NO poden importar:
- fastapi
- workers
- Telegram-specific libraries
- D1 bindings
- Vectorize bindings
- HTTP clients
```

`application/` pot dependre de `ports/`, però no de les implementacions.

Els adapters converteixen formats externs a contractes interns tan aviat com sigui possible.

No posar SQL als use cases.

No posar prompts directament als endpoints FastAPI.

No posar callbacks Telegram dins de `knowledge_update.py`.

No posar `if source_type == "telegram"` dins del core, excepte en polítiques explícites de provenance/authority que treballin amb l'enum de domini `SourceType`.

## Pydantic i domain

Pydantic s'ha d'usar fortament a les fronteres:

```text
Telegram payload -> Pydantic DTO -> domain/application
Workers AI JSON  -> Pydantic DTO -> application
CLI args         -> Typer/Pydantic -> application
```

Les entitats de domini poden ser `dataclass`/Enum de stdlib o Pydantic si redueix clarament boilerplate. La regla important és **no dependre de frameworks de transport o infraestructura**, no perseguir puresa acadèmica.

## Un sol desplegable

Clean Architecture **NO significa microserveis**.

V1:

```text
1 repo
1 Worker
1 D1
1 Vectorize index
1 Telegram bot
```

Els mòduls són separats per responsabilitat, no per procés.

---

# 3.4. Docker

Afegir obligatòriament:

```text
Dockerfile
.dockerignore
```

**No s'utilitza Docker Compose.** No hi ha `compose.yaml`; totes les comandes de
contenidor són `docker build` i `docker run` directes.

El `Dockerfile` de desenvolupament ha d'incloure:

- Python 3.13;
- `uv`;
- Node LTS compatible amb Wrangler/pywrangler;
- dependències del projecte;
- usuari no-root si no complica el runtime local.

No instal·lar bases de dades locals que no s'utilitzen en producció.

El Worker s'ha de provar amb `pywrangler dev` dins del contenidor perquè això executa l'entorn local compatible amb Workers/Pyodide, en comptes d'arrencar simplement `uvicorn`.

`uvicorn` es pot usar per tests ràpids de FastAPI si convé, però **no és el test de compatibilitat del runtime**.

El Worker local s'executa amb `docker run` directe (no hi ha Compose):

```bash
docker build -t knowledge-bot:dev .

docker run --rm -it -p 8787:8787 -v "$PWD":/workspace -w /workspace \
  knowledge-bot:dev uv run pywrangler dev --local --ip 0.0.0.0 --port 8787
```

Ajustar la sintaxi exacta al CLI vigent si `pywrangler` no accepta aquests flags directament; no inventar opcions.

## Smoke test obligatori de dependències

Abans d'avançar més enllà del skeleton, provar dins de `pywrangler dev`:

```python
import fastapi
import pydantic
import loguru
```

i exposar temporalment `/smoke/deps`.

Si Loguru no funciona sota Pyodide:

1. documentar l'error real;
2. mantenir una interfície `AppLogger`;
3. substituir només l'adapter de logging per stdlib `logging`;
4. no tocar application/domain.

No assumir incompatibilitat sense executar aquest test.

---

# 3.5. Logging amb Loguru

Configurar Loguru en un únic mòdul:

```text
src/infrastructure/logging.py
```

No configurar sinks des de múltiples fitxers.

## Desenvolupament

Format humà, nivell `DEBUG`.

## Producció

Enviar només a stdout/stderr i preferir JSON estructurat.

No escriure fitxers locals: el filesystem del Worker no és un sink persistent.

Camps contextuals recomanats:

```text
request_id
telegram_update_id
conversation_id
message_id
use_case
source_type
feedback_id
qa_id
duration_ms
```

Exemple conceptual:

```python
with logger.contextualize(
    request_id=request_id,
    conversation_id=conversation_id,
):
    logger.info("question_flow_started")
```

## PII / contingut

Per defecte NO loguejar:

```text
message.text
answer text complet
sender name
username
phone
raw Telegram update
raw WhatsApp line
AI prompt complet
```

Es poden loguejar:

```text
text_length
content_type
hash/id pseudonimitzat
retrieval_count
similarity
authority
decision
model
tokens/usage si està disponible
```

Afegir un helper explícit per redacció de metadata externa.

## Error logging

`logger.exception(...)` a les fronteres.

El domain no ha d'importar Loguru. El logging és cross-cutting però s'injecta o s'executa als use cases/adapters; les entitats de domini no emeten logs.

---

# 4. Estructura del repositori

Implementar una estructura similar a aquesta i respectar-ne la direcció de dependències:

```text
knowledge-bot/
├── Dockerfile
├── .dockerignore
├── .gitignore
├── pyproject.toml
├── uv.lock
├── wrangler.jsonc
├── README.md
├── AGENTS.md
│
├── docs/
│   ├── setup.md
│   ├── usage.md
│   ├── knowledge-base.md
│   ├── operations.md
│   └── development.md
│
├── migrations/
│   ├── 0001_initial.sql
│   └── 0002_indexes.sql
│
├── src/
│   ├── entry.py
│   ├── cli.py
│   │
│   ├── domain/
│   │   ├── entities.py
│   │   ├── value_objects.py
│   │   ├── enums.py
│   │   └── policies.py
│   │
│   ├── application/
│   │   ├── ingest.py
│   │   ├── answer_question.py
│   │   ├── submit_feedback.py
│   │   ├── review_correction.py
│   │   ├── update_knowledge.py
│   │   └── reindex.py
│   │
│   ├── ports/
│   │   ├── repositories.py
│   │   ├── vector_store.py
│   │   ├── classifier.py
│   │   ├── generator.py
│   │   ├── transport.py
│   │   ├── media.py
│   │   └── clock.py
│   │
│   ├── contracts/
│   │   ├── messages.py
│   │   ├── ai.py
│   │   ├── telegram.py
│   │   └── api.py
│   │
│   ├── adapters/
│   │   ├── inbound/
│   │   │   ├── fastapi_routes.py
│   │   │   ├── telegram.py
│   │   │   ├── web_snapshot.py
│   │   │   └── whatsapp_export.py
│   │   └── outbound/
│   │       ├── telegram.py
│   │       └── noop_media.py
│   │
│   └── infrastructure/
│       ├── cloudflare/
│       │   ├── d1.py
│       │   ├── vectorize.py
│       │   └── workers_ai.py
│       ├── logging.py
│       └── settings.py
│
├── data/
│   ├── raw/
│   │   └── .gitkeep
│   ├── seed/
│   │   ├── bhc_fcb_qa.json
│   │   └── whatsapp_messages.jsonl
│   └── fixtures/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── architecture/
│   └── fixtures/
│
└── evals/
    ├── classifier.yaml
    ├── retrieval.yaml
    ├── answers.yaml
    ├── abstention.yaml
    ├── conflicts.yaml
    └── corrections.yaml
```

`docs/` és la documentació operativa (desplegar, usar, mantenir, desenvolupar).
És també el material que el bot ha de poder llegir per explicar-se a si mateix
(§53.2).

Afegir tests d'arquitectura simples que detectin imports prohibits entre capes. No cal introduir una llibreria pesada: es pot inspeccionar l'AST/import graph amb Python estàndard.

# 5. Contracte intern normalitzat

Tot canal ha d'acabar generant el mateix objecte.

```python
class NormalizedMessage(BaseModel):
    id: str
    source_type: Literal["telegram", "whatsapp", "web"]
    source_message_id: str | None
    conversation_id: str
    sender_id: str | None
    sender_is_admin: bool
    timestamp: datetime
    text: str | None

    content_type: Literal[
        "text",
        "image",
        "document",
        "audio",
        "video",
        "unknown",
    ]

    reply_to_message_id: str | None = None
    attachments: list["AttachmentRef"] = []
    metadata: dict = {}
```

I:

```python
class AttachmentRef(BaseModel):
    kind: str
    external_id: str | None
    file_name: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    size_bytes: int | None
    processing_status: Literal["unprocessed", "processed", "ignored"]
```

El `core` només treballa amb aquests objectes.

---

# 6. Suport per imatges: v1 vs v2

## V1

Quan arribi una foto de Telegram:

1. guardar el missatge;
2. crear registre `attachment`;
3. guardar:
   - `file_id`;
   - `file_unique_id`;
   - mida si existeix;
   - dimensions;
4. marcar:

```text
processing_status = "unprocessed"
```

5. **NO cridar `getFile`.**
6. **NO descarregar binari.**
7. **NO cridar cap model multimodal.**
8. **NO crear embedding del contingut visual.**

Si la foto té caption, el caption sí que es tracta com text normal.

## V2

Deixar preparada aquesta interfície:

```python
class MediaProcessor(Protocol):
    async def process(
        self,
        attachment: AttachmentRef,
    ) -> MediaKnowledgeResult:
        ...
```

A v1 implementar:

```python
class NoOpMediaProcessor:
    ...
```

Així una v2 podrà afegir:

```text
Telegram file
    ↓
download
    ↓
vision model / OCR
    ↓
NormalizedKnowledge
    ↓
mateix pipeline
```

sense modificar TelegramAdapter ni el core.

---

# 7. Base inicial de coneixement

## 7.1 Web Q&A BHC/FCB

Font:

```text
https://bhc-fcb.netlify.app/
```

La pàgina és **read-only des del punt de vista del bot**.

No intentar mai modificar-la.

El snapshot actual conté, a data de disseny:

```text
36 preguntes
2 en revisió
```

Inclou seccions com:

- Inscripcions i pagaments
- Organització
- Instal·lacions
- Equipament
- Comunicació
- Salut / certificat mèdic
- Servei d'autobús
- Partits / esdeveniments
- Minis

El parser ha de conservar explícitament:

```python
class SeedQA(BaseModel):
    source_url: str
    source_anchor: str | None
    section: str
    question: str
    answer: str
    status: Literal["published", "in_review"]
    retrieved_at: datetime
```

Exemple real de coneixement inicial:

```text
Pregunta:
Com i quan s'ha de demanar l'equipament?

Resposta:
el club avisa a l'inici de temporada,
els infants es proven les talles,
i posteriorment el delegat envia una llista
perquè cada família faci la comanda.
```

### Important

Una pregunta marcada `En revisió`:

```text
Quan arriben els equipaments?
```

NO s'ha de convertir en una resposta fiable.

Ha de quedar amb:

```text
authority = low
answerability = uncertain
```

i ha de servir com un cas d'eval d'abstenció.

---

## 7.2 Snapshot, no scraping runtime

No descarregar ni parsejar el web en cada pregunta.

Implementar:

```bash
uv run python scripts/snapshot_web.py \
  --url "https://bhc-fcb.netlify.app/" \
  --out data/seed/bhc_fcb_qa.json
```

Aquest script s'executa fora del Worker.

Ha de:

1. descarregar HTML;
2. parsejar Q&A;
3. conservar anchors reals si existeixen;
4. conservar `En revisió`;
5. guardar `retrieved_at`;
6. calcular SHA-256 de l'HTML;
7. escriure JSON determinista;
8. fallar si extreu un nombre sospitosament petit de Q&A.

Acceptance check inicial:

```text
qa_count >= 30
```

No hardcodejar "36" com a invariant etern.

---

# 7.3. Com obtenir l'export de WhatsApp

Per al prototip **exportar sense fitxers multimèdia**. La v1 no processarà imatges i necessitem maximitzar la simplicitat i la quantitat de text disponible.

## Android

Des del telèfon:

```text
WhatsApp
→ obrir el grup
→ ⋮
→ Més
→ Exporta el xat
→ Sense fitxers / Sense multimèdia
```

Guardar o compartir el fitxer resultant amb l'ordinador.

## iPhone

Des del telèfon:

```text
WhatsApp
→ obrir el grup
→ tocar el nom del grup
→ Exportar xat
→ Sense multimèdia
```

El resultat pot arribar com ZIP; descomprimir-lo localment.

## Input del projecte

Copiar només el fitxer de text exportat a:

```text
data/raw/whatsapp.txt
```

No commitejar aquest fitxer al repositori.

Afegir a `.gitignore`:

```gitignore
data/raw/*
!data/raw/.gitkeep
```

Import:

```bash
uv run kb import-whatsapp \
  data/raw/whatsapp.txt \
  --out data/seed/whatsapp_messages.jsonl
```

La comanda ha de mostrar abans d'escriure:

```text
messages parsed
date range
unique pseudonymized senders
text messages
media placeholders
system messages
parse failures
```

i ha de fallar si el percentatge de línies no parsejades supera un threshold raonable configurable.

No usar una còpia de seguretat xifrada de WhatsApp/Google Drive/iCloud. L'input és l'**export de xat en text**, no un backup.

---

# 8. Import inicial de WhatsApp

L'extracte de WhatsApp encara no forma part del runtime.

Implementar un import offline.

Input esperat:

```text
data/raw/whatsapp.txt
```

Output:

```text
data/seed/whatsapp_messages.jsonl
```

El parser ha de tolerar com a mínim formats Android/iOS tipus:

```text
19/09/26, 09:32 - Nom: Missatge
```

i:

```text
[19/09/26, 09:32:12] Nom: Missatge
```

Ha de suportar:

- missatges multilínia;
- missatges sense autor;
- canvis de sistema;
- emojis;
- català;
- castellà;
- URLs;
- placeholders de media.

No cal suport perfecte de tots els exports mundials.

## Mitjans dins l'export

Exemples:

```text
<Multimedia omès>
image omitted
IMG-20260919-WA0003.jpg
```

Guardar-los com:

```json
{
  "content_type": "image",
  "attachments": [{
    "kind": "image",
    "file_name": "...",
    "processing_status": "unprocessed"
  }]
}
```

No processar-ne el contingut.

---

# 9. Model de dades D1

## sources

```text
id
source_type
external_ref
title
canonical_url
authority
is_mutable
created_at
```

`source_type`:

```text
web_seed
whatsapp_import
telegram
admin
```

---

## conversations

```text
id
source_id
external_id
title
created_at
```

---

## messages

```text
id
source_id
conversation_id
external_id
sender_hash
sender_is_admin
sent_at
text
content_type
reply_to_message_id
created_at
```

No cal guardar:

- username;
- nom complet;
- avatar;
- número de telèfon.

---

## attachments

```text
id
message_id
kind
external_file_id
external_unique_id
file_name
mime_type
width
height
size_bytes
processing_status
created_at
```

---

## qa_items

```text
id
canonical_key
canonical_question
status
current_version_id
created_at
updated_at
```

`status`:

```text
active
under_review
superseded
```

---

## qa_versions

```text
id
qa_id
answer
authority
confidence
origin
created_by
supersedes_version_id
created_at
```

`origin`:

```text
web_seed
auto_generated
admin_approved
```

---

## qa_evidence

```text
qa_version_id
evidence_type
evidence_id
```

Pot apuntar a:

```text
message
web_qa
qa_version
```

---

## bot_answers

```text
id
conversation_id
user_message_id
telegram_bot_message_id
question
answer
answer_mode
confidence
qa_version_id
sources_json
created_at
```

---

## feedback

```text
id
bot_answer_id
qa_id
reporter_hash
status
proposed_answer
admin_edited_answer
created_at
resolved_at
```

`status`:

```text
awaiting_proposal
pending_admin
approved
rejected
```

---

## recap_state

```text
conversation_id
last_sent_at
```

---

# 10. Autoritat de les fonts

No assumir que qualsevol missatge del grup és cert.

Assignar autoritat inicial:

```text
100  admin_approved
 95  missatge Telegram de l'ADMIN
 90  web_seed published
 60  resposta auto-generada amb >= 2 evidències coherents
 50  WhatsApp import
 40  Telegram usuari normal
 30  web_seed in_review
```

Aquests valors són política del prototip, no probabilitats.

## Regles

### Correcció humana aprovada

Sempre domina versions automàtiques o fonts antigues.

### Web vs missatge de grup

Si un missatge normal contradiu un Q&A publicat del web:

```text
NO substituir automàticament.
```

Crear possible conflicte/revisió.

### Missatge de l'admin

Pot actualitzar automàticament coneixement si:

- és inequívoc;
- no és només una pregunta;
- l'extractor produeix un Q&A coherent.

### `in_review`

Mai usar-lo com a única evidència d'una resposta afirmativa.

---

# 11. Embeddings

Model:

```text
@cf/google/embeddinggemma-300m
```

Dimensions:

```text
768
```

Crear **un únic índex Vectorize**:

```text
knowledge-v1
```

Metric:

```text
cosine
```

Metadata mínima del vector:

```json
{
  "kind": "qa_version",
  "object_id": "...",
  "status": "active",
  "authority": 90
}
```

o:

```json
{
  "kind": "message",
  "object_id": "...",
  "source_type": "telegram"
}
```

No crear un índex separat per cada canal.

## Metadata indexes (obligatori)

Vectorize **no filtra per metadata sense un metadata index**. Cal crear-ne un per
cada propietat filtrada:

```bash
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name kind --type string
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name status --type string
```

Sense això, les consultes amb `filter` retornen zero resultats. El `kind`
distingeix `qa_version` de `message`; el `status` filtra els Q&A actius.

---

# 12. Classificador de missatges

No utilitzar GLM per classificar tots els missatges.

Fer un classificador semàntic simple sobre EmbeddingGemma.

## Labels independents

No són mútuament excloents:

```python
class IntentScores(BaseModel):
    question: float
    knowledge_update: float
    correction: float
    chitchat: float
```

Exemple:

```text
"No, finalment és dijous, oi?"

question          high
knowledge_update  medium
correction        high
chitchat          low
```

## Implementació v1

Guardar prototips textuals:

```text
question:
- "què hem de portar demà?"
- "a quina hora entrenen?"
- "on es compra l'equipament?"
...

knowledge_update:
- "recordeu que demà no hi ha entrenament"
- "la reunió serà a les cinc"
...

correction:
- "no, finalment és dijous"
- "han canviat l'hora"
...

chitchat:
- "gràcies!"
- "perfecte"
- "😂"
...
```

Embeddar els prototips una vegada.

Per cada missatge:

1. embedding;
2. similitud amb prototips;
3. obtenir `max_similarity` per label;
4. aplicar thresholds configurables.

**No anomenar aquests scores "probabilitats".**

## Detecció de preguntes vs. decisió de respondre

El classificador detecta preguntes per **indexar i prioritzar** missatges, però la
decisió de respondre és independent i explícita.

El bot respon **només** quan el missatge l'adreça:

```text
/@botname ...            (menció al bot)
resposta a un missatge del bot
missatge directe (DM) al bot
/ask ...   o   /ask@botname ...
```

Una pregunta sense cap d'aquests senyals **no** genera resposta; es pot recollir
al resum periòdic (§26.1).

Això evita que el bot interrompi converses normals del grup. El detector semàntic
continua existint per classificar i ordenar el coneixement, no per desencadenar
respostes.

## Future replacement

Definir port:

```python
class MessageClassifier(Protocol):
    async def classify(self, text: str) -> IntentScores:
        ...
```

De manera que després es pugui substituir per:

```text
fine-tuned XLM-R
Jev
GLiNER
altre classifier
```

sense tocar el core.

---

# 13. Ingesta de missatges

Per defecte el bot **no escolta** el grup: només processa els missatges que
l'adrecen (menció, resposta al bot, DM o `/ask`). Aquests sempre s'ingereixen i es
responen.

El **background listener** és opcional i està **desactivat per defecte**
(`BACKGROUND_LISTENER_ENABLED=false`). Quan s'activa, també s'ingereixen tots els
missatges no adreçats (per construir la base de coneixement), però **no es
responen mai**.

Pipeline per cada missatge textual acceptat:

```text
Telegram
   ↓
verify webhook
   ↓
verify allowed chat
   ↓
normalize
   ↓
intake decision (ignore / ingest / answer)
   ↓
persist D1
   ↓
embedding
   ↓
Vectorize
   ↓
classify
```

Per reduir soroll en retrieval, Vectorize metadata ha de permetre filtrar o penalitzar `chitchat`.

---

# 14. Flow de pregunta

## Seqüència

```text
User question
     |
     v
Telegram webhook
     |
     v
normalize + persist
     |
     v
trigger gate (adreçat al bot?)
     |
     v
question detector
     |
     v
embed question
     |
     +----------------------+
     |                      |
     v                      v
retrieve Q&A            retrieve messages
     |                      |
     +----------+-----------+
                |
                v
          evidence gate
                |
         +------+------+
         |             |
         v             v
    answerable      insufficient
         |             |
         v             v
  direct answer /   abstain
  GLM synthesis
         |
         v
 source validator
         |
         v
 Telegram answer
         |
         v
[⚠️ Està malament?]
```

---

# 15. Retrieval

Per cada pregunta:

## Q&A

Recuperar:

```text
top_k = 5
```

amb filtre:

```text
kind = qa_version
status = active
```

## Missatges

Recuperar:

```text
top_k = 8
```

Prioritzar:

- missatges recents;
- admin;
- imported WhatsApp;
- Telegram;
- no chitchat.

No enviar 100 missatges al model.

---

# 16. Evidence gate

Abans de generar resposta, decidir si existeix evidència suficient.

No confiar en `confidence` auto-declarada pel LLM.

## Cas A — strong Q&A match

Si hi ha Q&A actiu amb similarity alta i no està `under_review`:

```text
respondre directament des del Q&A
```

Es pot fer una petita reformulació, però preferiblement no cridar GLM.

## Cas B — múltiples missatges consistents

Si no hi ha Q&A però hi ha context coherent:

```text
GLM synthesis
```

i guardar una nova QA auto-generada si passa validacions.

## Cas C — conflicte

Si:

```text
web published says X
message says NOT X
```

i el missatge no és admin:

```text
abstain + optional admin review
```

## Cas D — no evidència

Respondre, en català o castellà segons la pregunta:

```text
No tinc prou informació fiable per respondre-ho.
```

No inventar.

---

# 17. Prompt generatiu

GLM només ha de veure:

- pregunta;
- Q&A recuperats;
- missatges recuperats;
- identificadors de font;
- instruccions estrictes.

Prompt conceptual:

```text
You answer questions using ONLY the evidence below.

Rules:
1. Do not add facts not supported by evidence.
2. If evidence conflicts materially, return INSUFFICIENT_EVIDENCE.
3. If the answer is unknown, return INSUFFICIENT_EVIDENCE.
4. Prefer higher-authority evidence.
5. A source marked "in_review" cannot alone establish a fact.
6. Return only JSON matching the requested schema.
7. source_ids must only contain IDs from the supplied evidence.
8. Answer in the language of the user's question.

QUESTION:
...

EVIDENCE:
...

OUTPUT:
{
  "status": "answered" | "insufficient",
  "answer": "...",
  "source_ids": ["..."]
}
```

Validar output amb Pydantic.

Màxim:

```text
1 retry
```

si el JSON és invàlid.

No fer loops autònoms.

---

# 18. Validació de fonts

Abans d'enviar:

```python
for source_id in model_output.source_ids:
    assert source_id in retrieved_source_ids
```

Si el model cita una font no proporcionada:

```text
reject output
```

No enviar-la.

---

# 19. Format de resposta Telegram

Exemple:

```text
La comanda de l'equipament es fa després que els nens i nenes
s'hagin pogut provar les talles. Després el delegat envia una
llista perquè cada família indiqui la talla i, si escau, el dorsal.

Fonts:
• Q&A · Equipament · "Com i quan s'ha de demanar l'equipament?"
  https://bhc-fcb.netlify.app/#qa-equipament-com-demanar

[⚠️ Està malament?]
```

Si la font és un missatge sense URL pública:

```text
Fonts:
• Grup · ·a10cb7 · 18/09/2026 14:29
```

Regla de citació (decidida):

- **Web**: URL **exacta de l'entrada**, amb l'àncora (`...#qa-equipament-com-demanar`),
  no la URL global del lloc, si l'àncora existeix. Més la data.
- **Grup**: **autor i data amb hora** (nom visible de l'autor · DD/MM/YYYY HH:MM).
- **Correcció aprovada**: **autor de la proposta i data de la proposta**
  (no la URL web: el text ja no és el del web).

La procedència viu a la versió, no a una constant global. `qa_versions.source_url`
és la URL exacta de l'entrada web; `qa_versions.author` és qui va proposar la
correcció. Exactament una de les dues identifica l'origen, així una citació mai
pot reclamar la font equivocada. La data de la correcció és el moment de la
proposta (`feedback.proposed_at`), no el de l'aprovació.

---

# 20. Creixement automàtic del Q&A

Si una pregunta:

- no té strong Q&A match;
- és contestable a partir de context;
- la resposta passa source validation;
- no hi ha conflictes;

crear:

```text
qa_item
+
qa_version origin=auto_generated
```

Authority:

```text
60
```

Això converteix gradualment les converses en Q&A.

En fase 2 la web simplement llegirà aquests registres.

---

# 21. Flow `Està malament?`

Tot dins Telegram.

## Pas 1 — resposta normal

El bot envia resposta amb:

```text
[⚠️ Està malament?]
```

Callback data:

```text
feedback:start:<bot_answer_id>
```

---

## Pas 2 — usuari marca incorrecte

Crear:

```text
feedback.status = awaiting_proposal
```

El bot respon amb `ForceReply`:

```text
Què corregiries? Escriu la resposta correcta o explica què està malament.
```

---

## Pas 3 — usuari proposa correcció

Guardar:

```text
feedback.proposed_answer
feedback.status = pending_admin
```

Respondre al reporter:

```text
Gràcies. Ho he enviat a revisió.
```

---

# 22. Revisió per l'admin

Enviar en privat a `ADMIN_TELEGRAM_USER_ID`:

```text
⚠️ Correcció proposada

Pregunta:
Com es demana l'equipament?

Resposta actual:
...

Proposta:
...

Fonts utilitzades:
...

[✅ Aprovar] [✏️ Editar] [❌ Rebutjar]
```

Callbacks:

```text
feedback:approve:<feedback_id>
feedback:edit:<feedback_id>
feedback:reject:<feedback_id>
```

---

# 23. Aprovar

En aprovar:

1. començar transacció lògica;
2. crear nova `qa_version`;
3. `origin = admin_approved`;
4. `authority = 100`;
5. marcar la versió anterior com a superseded;
6. apuntar `qa_items.current_version_id` a la nova;
7. actualitzar Vectorize;
8. marcar feedback `approved`;
9. intentar editar el missatge original del bot;
10. confirmar a l'admin.

Important:

```text
NO modificar el web Netlify.
```

Si el Q&A original venia del web, crear un **override local**.

---

# 24. Editar

Si l'admin prem:

```text
✏️ Editar
```

el bot privat fa:

```text
Envia'm el text correcte.
```

amb `ForceReply`.

El flux de correcció passa **sempre per xats privats**, mai pel grup:

1. **Qualsevol membre del grup** pot prémer `⚠️ Està malament?` i proposar una
   correcció. El bot demana la proposta al **DM del reporter**.
2. En rebre-la, el bot envia la **revisió al DM de l'admin** amb botons
   `✅ Aprovar` / `✏️ Editar` / `❌ Rebutjar`.
3. `✏️ Editar` mostra la proposta actual i demana el text corregit; el resultat
   torna al DM de l'admin amb els botons d'aprovar/rebutjar.
4. **Només l'admin** pot confirmar (aprovar/editar/rebutjar). L'adaptador rebutja
   qualsevol callback de confirmació que no vingui de `ADMIN_TELEGRAM_USER_ID`.
5. En aprovar, el reporter rep un **missatge privat de gràcies**.

AGENTS: el codi no pot exposar el nom de l'autor a cap log.

Quan l'admin respon:

1. guardar `admin_edited_answer`;
2. mostrar preview;
3. oferir:

```text
[✅ Aprovar aquesta versió] [❌ Cancel·lar]
```

No aprovar automàticament el primer text rebut.

---

# 25. Rebutjar

Marcar:

```text
feedback.status = rejected
```

No modificar Q&A.

Opcional:

```text
notify reporter
```

---

# 26. Actualitzar resposta ja enviada

Si és tècnicament possible perquè és un missatge del bot, cridar:

```text
editMessageText
```

i transformar:

```text
Resposta antiga...
```

en:

```text
✅ Resposta corregida

Nova resposta...

Corregit per l'administrador.
```

Si l'edició falla:

```text
enviar un nou missatge de correcció
```

No fer rollback del Q&A perquè l'edició visual falli.

---

# 26.1. Resum periòdic de preguntes

Per evitar omplir el xat i perquè les preguntes no respostes no es perdin, el bot
publica un resum periòdic al grup.

## Contingut

- Preguntes fetes durant la finestra i la resposta donada.
- Preguntes **sense resposta** (abstenció) marcades com a pendents.
- Per a les preguntes pendents, el resum ha d'intentar donar-hi resposta (un cop
existeixi el pipeline de resposta); mentrestant es mostren com a pendents.

## Configuració

```text
RECAP_ENABLED=true
RECAP_INTERVAL_HOURS=24
RECAP_LANGUAGE=ca
```

## Disparador: oportunitat (decidit)

Els Python Workers de Cloudflare **només exposen el handler `fetch`**: el SDK de
Python no ofereix `scheduled`, i la documentació no documenta cron per a Python.
Per tant, el resum no es pot programar amb un Cron Trigger dins el mateix Worker.

Decisió: **disparador oportunista**. A cada update acceptat, abans o després de
processar el missatge, es comprova `is_recap_due(...)` i, si toca, es publica el
resum. Un sol Worker, gratuït i event-driven.

Estat: taula `recap_state(conversation_id, last_sent_at)` per no enviar-lo més
d'un cop per interval.

(Opcional futur: un programador extern pot cridar `POST /internal/recap` amb
`X-Internal-Key` per cobrir dies sense activitat.)

---

# 27. Telegram Adapter

## Inbound

Responsabilitats:

- verificar `X-Telegram-Bot-Api-Secret-Token`;
- verificar `chat.id`;
- convertir Update → NormalizedMessage;
- detectar:
  - message;
  - callback_query;
  - reply;
  - commands.

No decidir coneixement.

## Outbound

Interfície:

```python
class MessageTransport(Protocol):
    async def send_message(...): ...
    async def edit_message(...): ...
    async def answer_callback(...): ...
```

Implementació v1:

```text
TelegramTransport
```

En fase futura:

```text
EmailTransport
WhatsAppTransport
```

---

# 28. HTTP routes del Worker

Implementar només:

```text
POST /telegram/webhook
GET  /healthz
POST /internal/seed
POST /internal/reindex
```

`/internal/*` requereix:

```text
X-Internal-Key
```

No afegir una API pública genèrica.

---

# 29. Secrets / variables

Secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
INTERNAL_ADMIN_KEY
```

Vars:

```text
ALLOWED_TELEGRAM_CHAT_IDS
ADMIN_TELEGRAM_USER_ID

EMBEDDING_MODEL=@cf/google/embeddinggemma-300m
GENERATION_MODEL=@cf/zai-org/glm-4.7-flash

QUESTION_THRESHOLD=...
DIRECT_QA_THRESHOLD=...
SYNTHESIS_THRESHOLD=...
QA_TOP_K=...
MESSAGE_TOP_K=...
CONFLICT_MARGIN=...

RECAP_ENABLED=true
RECAP_INTERVAL_HOURS=24
RECAP_LANGUAGE=ca

BACKGROUND_LISTENER_ENABLED=false
```

Els thresholds han de quedar configurables.

No hardcodejar tokens.

---

# 30. Guard de cost 0 €

Implementar explícitament:

```python
ALLOWED_AI_MODELS = {
    "@cf/google/embeddinggemma-300m",
    "@cf/zai-org/glm-4.7-flash",
}
```

Qualsevol model no inclòs:

```text
raise ConfigurationError
```

No implementar:

```text
OpenAI fallback
Anthropic fallback
Gemini fallback
paid Workers fallback
```

Si Workers AI retorna quota exhausted:

1. guardar igualment el missatge;
2. no perdre l'event;
3. respondre, si era pregunta:

```text
Ara mateix no puc consultar la base de coneixement. Torna-ho a provar més tard.
```

4. loggear `quota_exhausted`.

---

# 31. Política de privacitat mínima

Tot i que el prototip sigui poc sensible:

- **excepció decidida:** es guarda el **nom visible de l'autor** (`messages.sender_name`)
  i, per a l'export de WhatsApp, el nom tal com apareix a l'export. És necessari per
  citar la font del grup (`• Grup · <autor> · <data hora>`). El nom només s'usa per
  a la citació: **mai** es logueja ni s'envia a cap model;
- no guardar avatar;
- el hash pseudònim (`sender_hash`) es conserva igualment com a identificador estable;
- conservar `ADMIN_TELEGRAM_USER_ID` només com secret/config;
- no enviar dades a serveis AI diferents de Workers AI;
- no processar imatges.

Nota: si l'export de WhatsApp no té el contacte desat, l'autor és el número de telèfon
(tal com apareix a l'export).

excepte si s'ha configurat explícitament com a trusted/admin sender.

---

# 32. Tests unitaris

Com a mínim:

## Telegram parsing

```text
test_text_message_normalization
test_reply_normalization
test_photo_is_metadata_only
test_caption_is_kept_as_text
test_callback_normalization
test_disallowed_chat_is_ignored
test_invalid_webhook_secret_is_rejected
```

## WhatsApp parser

```text
test_android_format
test_ios_format
test_multiline_message
test_media_placeholder
test_system_message
test_unicode_and_emoji
```

## Web parser

```text
test_extracts_questions
test_preserves_sections
test_preserves_in_review
test_preserves_source_anchor_when_available
test_rejects_suspiciously_empty_snapshot
```

## Classifier

```text
test_question
test_statement
test_correction
test_chitchat
```

## Trigger

```text
test_bare_question_is_not_addressed
test_mention_triggers_reply
test_reply_to_bot_triggers_reply
test_direct_message_triggers_reply
test_slash_ask_triggers_reply
test_disallowed_chat_is_ignored
test_invalid_webhook_secret_is_rejected
```

## Intake

```text
test_addressed_messages_are_answered
test_unaddressed_is_ignored_by_default
test_unaddressed_is_ingested_when_listening
test_addressed_is_answered_even_when_listening
```

## Recap

```text
test_recap_lists_questions_and_answers
test_recap_marks_abstentions_as_unanswered
test_recap_orders_by_time
test_recap_due_policy
test_recap_disabled
test_recap_is_sent_when_never_sent_before
test_recap_is_not_sent_twice_within_the_interval
test_recap_is_sent_again_after_the_interval
test_recap_language_is_configurable
```

## Retrieval

```text
test_qa_filter
test_message_filter
test_authority_ranking
test_in_review_is_penalized
```

## Grounding

```text
test_unknown_source_id_is_rejected
test_conflict_causes_abstention
test_no_evidence_causes_abstention
```

## Knowledge/versioning

```text
test_admin_version_supersedes_auto
test_web_seed_is_never_mutated
test_admin_override_of_web_creates_local_version
```

## Architecture

```text
test_domain_does_not_import_frameworks
test_application_does_not_import_infrastructure
test_core_does_not_import_telegram
test_no_sql_outside_infrastructure
```

## Logging

```text
test_logging_redacts_message_text
test_logging_includes_request_id
test_production_logging_uses_stdout
```

---

# 33. Integration test — flow de pregunta

Crear:

```text
tests/integration/test_question_flow.py
```

## Fixture

Seed Q&A:

```text
Q: Com i quan s'ha de demanar l'equipament?
A: El club avisa a inici de temporada, els infants es proven
   les talles i després el delegat envia una llista.
```

## Telegram event

```text
"@bot Quan hem de demanar la roba?"   (o /ask ...)
```

Afegir també un cas negatiu: una pregunta sense adreçar no ha de generar cap
resposta (només persistència).

## Test

1. POST fake Update al webhook.
2. Assert message persisted.
3. Assert classifier marks question.
4. Assert embedding called.
5. Assert retrieval gets the equipment Q&A.
6. Assert no unsupported sources enter prompt.
7. Assert bot sends response.
8. Assert response includes equipment fact.
9. Assert response references the Q&A/source URL.
10. Assert response has inline button:

```text
⚠️ Està malament?
```

11. Assert `bot_answers` row persisted.

Aquest test ha d'executar-se sense Internet:

```text
FakeEmbedder
FakeVectorStore
FakeGenerator
FakeTelegramTransport
InMemoryRepositories
```

---

# 34. Integration test — flow complet de correcció

Crear:

```text
tests/integration/test_correction_flow.py
```

Seqüència completa:

## 1

Usuari:

```text
Quan hem de demanar la roba?
```

Bot:

```text
Resposta A
[⚠️ Està malament?]
```

## 2

Simular callback:

```text
feedback:start:<answer_id>
```

Assert:

```text
feedback.status == awaiting_proposal
```

Bot:

```text
Què corregiries?
```

## 3

Usuari:

```text
En realitat aquest any la llista la passarà l'entrenador.
```

Assert:

```text
feedback.status == pending_admin
```

## 4

Assert que l'admin rep DM amb:

```text
Resposta actual
Proposta
Fonts
Approve/Edit/Reject
```

## 5

Simular:

```text
feedback:approve:<id>
```

Assert:

```text
new qa_version exists
authority == 100
origin == admin_approved
old version not current
feedback.status == approved
```

## 6

Assert Vectorize upsert/delete fake.

## 7

Assert que s'intenta editar la resposta antiga del bot.

## 8 — test crític

Enviar una nova pregunta semànticament equivalent:

```text
"Qui passarà la llista per demanar l'equipament?"
```

Assert:

```text
la resposta nova utilitza la correcció aprovada
NO la versió antiga
```

Aquest és el principal acceptance test funcional.

---

# 35. Integration test — imatges

Crear:

```text
test_image_v1.py
```

Enviar Telegram Update amb `photo`.

Assert:

```text
message persisted
attachment persisted
processing_status == unprocessed

FakeEmbedder.call_count == 0
FakeGenerator.call_count == 0
FakeMediaDownloader.call_count == 0
```

Si hi ha caption:

```text
caption sí que es pot indexar
```

però la imatge no.

---

# 36. Suite d'evals

Separar **tests de software** i **evals de comportament del bot**.

Els tests comproven que el codi funciona.

Els evals comproven que el bot respon bé.

---

# 37. Eval de detecció de preguntes

Fitxer:

```text
evals/classifier.yaml
```

Mínim:

```text
80 exemples
```

Distribució aproximada:

```text
20 question
20 knowledge_update
20 correction
20 chitchat
```

Català predominant, però incloure castellà.

Exemples:

```yaml
- text: "Demà entrenen?"
  labels: [question]

- text: "Recordeu que demà no hi ha entrenament"
  labels: [knowledge_update]

- text: "No, perdó, finalment és dijous"
  labels: [knowledge_update, correction]

- text: "Gràcies!"
  labels: [chitchat]
```

## Acceptance

Per `question`:

```text
precision >= 0.95
recall    >= 0.90
```

Prioritzar precision: és pitjor que el bot interrompi converses normals que perdre alguna pregunta.

Altres labels:

```text
macro F1 >= 0.85
```

Thresholds es calibren amb aquest fitxer.

---

# 38. Eval de retrieval

Fitxer:

```text
evals/retrieval.yaml
```

Mínim 30 queries.

Incloure paràfrasis dels Q&A reals.

Exemples:

```yaml
- query: "Quan podem demanar la roba?"
  expected_topic: "equipament-com-demanar"

- query: "Si plou fan entrenament?"
  expected_topic: "pluja"

- query: "On juguen quan són locals?"
  expected_topic: "installacions"

- query: "Com miro quan caduca el certificat mèdic?"
  expected_topic: "caducitat-certificat"
```

## Acceptance

```text
Recall@3 >= 0.90
Recall@5 >= 0.97
```

No avaluar només strings exactes.

---

# 39. Eval de resposta factual

Fitxer:

```text
evals/answers.yaml
```

Cada cas:

```yaml
- id: equipment_order
  question: "Com demano l'equipament?"
  must_include:
    - "provar"
    - "talla"
    - "llista"
  must_not_claim:
    - "es compra directament a la botiga del Barça"
  expected_source_type:
    - web_seed
```

Fer checks deterministes.

No dependre exclusivament d'un LLM judge.

---

# 40. Golden cases inicials suggerits

Incloure com a mínim:

### Equipament

1. `Com es demana l'equipament?`
2. `Quan es demana la roba?`
3. `On demano roba del BHC?`
4. `Quant costa la samarreta?`
5. `Com sé la talla?`

### Instal·lacions

6. `On entrenen?`
7. `Si plou entrenen igualment?`
8. `On juguen els partits de casa?`

### Comunicació

9. `Per què encara no soc al grup de WhatsApp?`
10. `Amb qui parlo si el delegat no ho sap?`

### Certificat

11. `És obligatori el certificat mèdic?`
12. `Com miro quan caduca?`
13. `He d'enviar l'informe al delegat?`

### Minis

14. `Els Minis juguen lliga?`
15. `Per què paguen fitxa federativa?`

### Unknown / abstention

16. `Quin és el dorsal del meu fill?`
17. `Demà vindrà l'entrenador Marc?`
18. `Quina talla exacta necessita el meu fill?`

### In review

19. `Quan arribarà l'equipament?`

Expected:

```text
NO afirmar una data.
```

### Cross-language

20. `¿Cuándo hay que pedir la equipación?`
21. `¿Entrenan si llueve?`

---

# 41. Eval d'abstenció

Fitxer:

```text
evals/abstention.yaml
```

Mínim 15 casos.

Incloure:

- informació absent;
- informació `in_review`;
- contradiccions;
- preguntes personals;
- dates futures no confirmades.

## Acceptance

```text
abstention recall >= 0.95
```

És preferible abstenció a al·lucinació.

---

# 42. Eval de conflictes

Fitxer:

```text
evals/conflicts.yaml
```

Exemple:

```yaml
question: "Quan arriba l'equipament?"

evidence:
  - authority: 30
    text: "Encara no està confirmat."
  - authority: 40
    text: "Crec que arriba dilluns."

expected:
  status: insufficient
```

Un altre:

```yaml
evidence:
  - authority: 90
    text: "L'entrenament és dimarts."
  - authority: 40
    text: "Em sembla que és dimecres."

expected:
  use_authority: 90
```

I:

```yaml
evidence:
  - authority: 90
    text: "L'entrenament és dimarts."
  - authority: 100
    text: "A partir d'ara serà dimecres."

expected:
  use_authority: 100
```

---

# 43. Eval de correccions

Fitxer:

```text
evals/corrections.yaml
```

Casos:

1. auto answer → correction → approve;
2. web answer → local override;
3. correction → reject;
4. correction → admin edit → approve;
5. two corrections sequentially;
6. later question uses newest approved version.

Acceptance:

```text
100% pass
```

No són probabilístics.

---

# 44. Citation / grounding eval

Per totes les respostes:

```text
citation_validity = 1.0
```

És a dir:

```text
cada source_id citat existeix
i estava dins del retrieval context
```

I:

```text
unsupported_source_rate = 0
```

---

# 45. Live evals

Crear dos nivells.

## Offline

```bash
uv run pytest
uv run python -m evals.run --mode offline
```

No toca Telegram ni Workers AI.

Obligatori en cada canvi.

## Live

```bash
uv run kb eval live
```

Utilitza:

- Workers AI real;
- embeddings reals;
- generador real.

Executar manualment abans de donar el prototip per bo.

No llançar live evals automàticament en cada commit per no consumir quota.

---

# 46. Métriques mínimes d'acceptació

Abans de considerar el prototip acabat:

```text
unit tests                         100% pass
integration tests                  100% pass
question precision                 >= 0.95
question recall                    >= 0.90
classifier macro F1                >= 0.85
retrieval Recall@3                 >= 0.90
retrieval Recall@5                 >= 0.97
abstention recall                  >= 0.95
citation validity                   1.00
unsupported source rate             0.00
correction flow                    100% pass
image no-processing flow           100% pass
```

No substituir aquests criteris per "sembla que funciona".

---

# 47. Model failure behavior

Si embedding falla:

```text
guardar missatge
no perdre update
no inventar resposta
```

Si generació falla:

```text
guardar pregunta
respondre error temporal
```

Si Telegram send falla:

```text
guardar answer state
log error
```

Si Vectorize falla:

```text
D1 continua sent source of truth
```

Si D1 falla:

```text
NO continuar com si res
```

---

# 48. Idempotència

Telegram pot reintentar webhooks.

Per tant:

```text
(source_type, external_message_id)
```

ha de ser únic.

Processar el mateix Update dues vegades no pot:

- duplicar message;
- duplicar answer;
- duplicar feedback;
- duplicar Q&A version.

---

# 49. D1 és source of truth

Vectorize és un índex derivat.

Si es perd o queda inconsistent:

```text
POST /internal/reindex
```

ha de poder reconstruir-lo des de D1.

No guardar informació que només existeixi a Vectorize.

---

# 50. Web fase 2 — deixar preparada l'API, però no construir-la

No implementar frontend.

Però el model de dades ha de permetre que després una web pugui llegir:

```text
qa_items
qa_versions
qa_evidence
feedback
```

Quan arribi la fase 2, una web podrà mostrar:

```text
Pregunta
Resposta actual
Última actualització
Fonts
[Està malament?]
```

sense migrar la base.

---

# 51. Adaptadors futurs

No implementar, només garantir que l'arquitectura ho permet.

## Email

```text
EmailAdapter
  ↓
NormalizedMessage
  ↓
mateix core
```

## WhatsApp

```text
WhatsAppRuntimeAdapter
  ↓
NormalizedMessage
  ↓
mateix core
```

## Slack

Mateix patró.

Cap d'aquests canals pot requerir canvis a:

```text
retrieval
classification
knowledge versioning
feedback
grounding
Q&A
```

---

# 52. Passos d'implementació per l'agent

Executar en aquest ordre.

## Milestone 1 — skeleton i entorn reproduïble

- iniciar projecte Python Worker;
- `uv`;
- `Dockerfile` + `.dockerignore` (sense Docker Compose);
- FastAPI + Pydantic;
- Loguru;
- Ruff;
- `ty`;
- pywrangler;
- `/healthz`;
- `/smoke/deps` temporal;
- tests bàsics;
- tests d'arquitectura.

Definition of done:

```text
docker build -t knowledge-bot:dev .
docker run --rm -it -p 8787:8787 -v "$PWD":/workspace -w /workspace knowledge-bot:dev uv run pywrangler dev --local --ip 0.0.0.0 --port 8787
/healthz returns 200
FastAPI/Pydantic/Loguru smoke test passes under pywrangler
ruff format --check passes
ruff check passes
ty check passes
pytest passes
```

---

## Milestone 2 — domain + D1

- models;
- migrations;
- repositories;
- idempotència;
- tests.

---

## Milestone 3 — Telegram ingest

- webhook;
- secret verification;
- allowed chat;
- normalization;
- text;
- images metadata-only;
- `/chatid`;
- `/whoami`.

No AI encara.

---

## Milestone 4 — seed web

- snapshot parser;
- JSON seed;
- D1 import;
- Q&A versioning.

---

## Milestone 5 — WhatsApp import

- parser;
- JSONL;
- D1 seed;
- fixtures.

---

## Milestone 6 — embeddings + Vectorize

- Workers AI binding;
- EmbeddingGemma;
- index;
- reindex endpoint;
- retrieval.

---

## Milestone 7 — classifier

- prototypes;
- embedding similarity;
- eval set;
- threshold calibration.

---

## Milestone 8 — QA answer flow

- retrieval;
- evidence gate;
- direct Q&A answer;
- GLM synthesis;
- source validator;
- Telegram answer.

Passar `test_question_flow`.

---

## Milestone 9 — correction flow

- feedback;
- ForceReply;
- admin DM;
- approve;
- reject;
- edit;
- version superseding;
- Vectorize update;
- edit original bot message.

Passar `test_correction_flow`.

---

## Milestone 10 — evals

- classifier;
- retrieval;
- answers;
- abstention;
- conflicts;
- correction suite;
- report Markdown.

---

## Milestone 11 — deploy real

- D1 remote;
- Vectorize remote;
- AI binding;
- secrets;
- webhook;
- test group;
- seed;
- live evals.

---

# 52.1. Quality gate obligatori

Cap milestone es considera acabat fins que:

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
```

retornen codi 0.

A CI, aquest és el gate mínim.

El codi públic del core, ports, DTOs i serveis ha d'estar tipat. Evitar `Any` llevat de fronteres externes inevitables (payload cru de Telegram/Cloudflare), i convertir aquestes dades a Pydantic tan aviat com sigui possible.

---

# 53. Definition of Done final

El prototip està acabat quan:

1. jo puc escriure al grup adreçant el bot:
   ```text
   @bhc_qa_testbot Quan s'ha de demanar l'equipament?
   ```

2. el bot respon amb informació correcta i una font.

3. puc prémer:
   ```text
   ⚠️ Està malament?
   ```

4. puc proposar una nova resposta.

5. jo rebo la proposta com a admin en privat.

6. puc:
   ```text
   Aprovar / Editar / Rebutjar
   ```

7. si aprovo, una nova pregunta equivalent rep la versió corregida.

8. una pregunta sense evidència produeix abstenció.

9. una foto queda registrada però no és processada.

10. els Q&A originals del web mai són modificats.

11. tot segueix dins del free tier i no existeix cap fallback de pagament.

12. `pytest` i els evals compleixen els thresholds definits.

13. `docker build` i l'execució del Worker amb `docker run` funcionen des d'un checkout net, sense Docker Compose.

14. Ruff i `ty` passen sense exclusions globals.

15. Els logs de producció són estructurats i no contenen text cru dels missatges ni PII.

16. Els tests d'arquitectura garanteixen que domain/application no depenen de Telegram, FastAPI ni Cloudflare.

17. Una pregunta sense adreçar el bot no genera resposta, i el resum periòdic es publica quan toca.

---

# 53.1. Estat actual de la implementació

Darrera actualització: **2026-09-21**. Aquest apartat és la font de veritat sobre què
està fet i què no; la resta del pla descriu el destí, no l'estat. Els milestones
M1–M6 i M8–M11 estan fets i desplegats; el pla original es considera implementat
en allà on aquest apartat no digui el contrari.

## Fet i verificat

- **M1** esquelet, `uv`, Docker, FastAPI, Loguru, Ruff, `ty`, `/healthz`. `make smoke` verd.
- **M2** domini, migracions `0001`–`0007`, repositoris D1, idempotència, fakes en memòria.
- **M3** ingesta Telegram: webhook, secret, xat permès, normalització, mitjans com a metadades.
- **M4** snapshot web: 36 Q&A, 2 `in_review`, 9 seccions.
- **M5** import WhatsApp: 75 missatges, 0 fallades (dates iOS i AM/PM).
- **M6** embeddings, Vectorize, reindex, retrieval.
- **M8** flow de resposta: evidence gate, Q&A directa, síntesi GLM, validació de fonts.
- **M9** flow de correcció: botó, DM del reporter, revisió a l'admin, aprovar/editar/rebutjar, versionat.
- **M11** desplegament real, secrets, webhook, seed, evals.
- **Guard de cost** (`ai_budget`): estima els neurons del dia i refusa evals/reindex
  abans d'esgotar la quota. Una pregunta d'usuari mai és refusada (degrada a error temporal).
- **Citacions amb procedència real**: web → URL exacta amb àncora; grup → autor i data;
  correcció → autor de la proposta i data de la proposta. La procedència viu a la versió.
- **Coneixement multi-grup (scopes)** (després del 2026-09-19): registre de grups
  (`kb group add`), seed amb `--scope`, retrieval que combina la capa global amb la
  del grup que pregunta. Una variant de grup sempre guanya la resposta global de la
  mateixa pregunta dins del seu grup, i l'admin tria l'abast (global o grup) en aprovar
  una correcció. Migració `0008`.
- **Renovació append-only de la base**: `kb seed --renew` afegeix una nova versió quan
  canvia la resposta (latest wins); els originals mai no s'esborren. La divergència
  amb correccions recents es veu al report de revisió.
- **Report de revisió humana**: `kb review` genera un Markdown de només lectura amb
  les divergències entre base i correccions, variants de grup, i renovacions que
  han sobreescrit correccions recents.
- **Batching de seed/reindex**: peticions en lots per cabre dins el límit de CPU del
  pla gratuït, amb cursors per reprendre si una petició pesada cau; reindex dirigit
  d'una sola versió després d'aprovar una correcció; texts de missatge capats a l'índex.
- **Gate d'accés per DM**: els missatges directes queden restringits a l'admin i els
  usuaris de `ALLOWED_TELEGRAM_USER_IDS`; el flux de correcció accepta sempre la
  resposta a una pregunta del bot, sense allowlist.
- **El bot s'explica a si mateix**: Q&A estàtic versionat a
  `data/seed/bot_self_qa.json` (generat dels docs, sense LLM en runtime), sembrat
  com a coneixement global amb `make seed-self-qa` i mantingut a cada tag
  (AGENTS.md §10.1). Respon "qui ets?", "com funciones?", el flux de correcció,
  els límits i l'abstenció. Resol §53.2 per la via estàtica.

Gates: **88 unitaris + 72 integració + 2 arquitectura** (162) verds, offline evals
4/4 (trigger 5/5, abstenció 16/16, citacions 5/5, seed versioning 4/4), `ruff` i
`ty` nets.

## Pendent (decidit, no fet)

- **M7 classificador** (§12): no hi ha mòdul ni calibratge. `evals/classifier.yaml`
  existeix sense res a provar. Es va ajornar perquè el listener està apagat i el bot
  només respon quan se l'adreça. És un milestone del pla, per tant està pendent.
- **M3 pendents**: `/chatid` i `/whoami` no implementats (el chat id es llegeix a
  mà del primer update; vegeu `docs/setup.md`).
- **§26 actualitzar la resposta enviada**: `edit_message` existeix al port de transport
  però **no es crida mai**. Després d'aprovar, el missatge original del grup continua
  mostrant la resposta antiga.
- **§43 eval de correccions**: falta `evals/corrections.yaml` (6 casos, 100%).
- **§42 eval de conflictes**: `evals/conflicts.yaml` existeix però **no està connectat**
  a `run_offline()`.
- **Logs de producció**: `configure_logging()` es crida sense arguments, així que la
  sortida no és JSON. A més **no hi ha cap crida `logger.*`** al codi d'aplicació: la
  privacitat es compleix de manera trivial, però falta l'observabilitat del §3.5.

## Bloquejat o no verificat

- **Llindars dels evals en viu (§46)**: `abstention/live` va quedar a **92%** amb
  objectiu ≥ 95% quan es va esgotar la quota.
- **El jutge condicional** (dues crides, només per a casos que passen els controls
  deterministes) **no s'ha provat mai en viu**.

## Fora d'abast a propòsit

§50 (API web fase 2) i §51 (adaptadors futurs): el pla diu preparar-los, no construir-los.

---

# 53.2. El bot que s'explica a si mateix — RESOLT (2026-09-21)

**Resolt per la via estàtica.** El bot respon "qui ets?", "com funciones?", com
es corregeix una resposta, quins són els seus límits i per què s'absté, a partir
d'un Q&A estàtic versionat: `data/seed/bot_self_qa.json`.

## Com funciona

1. El fitxer el genera el model de coding **a partir de `docs/` i `README.md`**
   en el moment del tag de release; no hi ha generació LLM en runtime ni en pipeline.
2. Es sembra com a Q&A **global** amb `make seed-self-qa`
   (`kb seed --qa data/seed/bot_self_qa.json`), amb el mateix pipeline que
   qualsevol altra Q&A del web: idempotent, versionat, i amb `--renew` quan
   canvia un text.
3. Retrieval, evidence gate i citació no tenen cap camí especial: el bot respon
   en la llengua de la pregunta, com ja fa, i pot citar l'entrada del doc d'origen
   (`source_url` apunta al fitxer de `docs/` corresponent al GitHub).
4. Les entrades no exposen internals (taules, secrets, claus de configuració).

## Per què no l'enfocament d'indexar `docs/` per seccions

L'enfocament proposat originalment (`kind=doc_section` a Vectorize, metadata per
secció, data del git) quedava descartat de moment perquè:

- afegeix un `kind` nou al retrieval i metadata indexes per resoldre un problema
  que un fitxer estàtic de 14 entrades cobreix;
- el contingut explicatiu del bot canvia poc i el volíem revisable per un humà
  abans de publicar-lo;
- la documentació ja viu al git; duplicar-la a l'índex trencaria "D1 és la font
  de veritat" per a material que no és coneixement del club.

Si el material creix o vol citar seccions exactes amb data del git, l'enfocament
de `doc_section` segueix sent l'upgrade path natural.

## Criteris d'acceptació

- "Qui ets?" / "Què saps fer?" → descriu el flow (respon, cita, es pot corregir).
- "Com corregeixo una resposta equivocada?" → descriu el flux per DM.
- Una capacitat inexistent ("processes fotos?") → negació clara.
- Manteniment lligat a `docs/`: AGENTS.md §10.1 obliga a actualitzar les entrades
  a la mateixa branca que el canvi de comportament, i a cada release tag.

---

# 54. Notes específiques per a l'agent de coding

- No sobreenginyar.
- FastAPI, Pydantic, Typer, Loguru, Ruff, ty, uv i Docker formen part explícita de l'stack.
- No afegir altres frameworks si no són estrictament necessaris.
- No convertir ports en serveis desplegats.
- No crear una web.
- No implementar autenticació d'usuaris.
- No processar multimèdia.
- No implementar fine-tuning.
- No afegir models alternatius automàtics.
- No canviar Cloudflare per un altre hosting.
- No introduir dependències pesades de ML dins del Worker.
- No executar models localment dins del Worker.
- No usar noms/PII com a feature de retrieval.
- No usar el text generat pel mateix bot com a evidència, excepte si ha estat convertit a `qa_version` amb provenance vàlida.
- D1 és la font de veritat.
- Vectorize és reconstruïble.
- Tota resposta ha de ser auditable fins a fonts originals.
- Tota correcció aprovada ha de ser versionada, mai fer UPDATE destructiu de l'històric.

---

# 55. Referències tècniques verificades

Cloudflare Python Workers:
https://developers.cloudflare.com/workers/languages/python/

FastAPI on Cloudflare Python Workers:
https://developers.cloudflare.com/workers/languages/python/packages/fastapi/

Cloudflare Python package support:
https://developers.cloudflare.com/workers/languages/python/packages/

Cloudflare local development:
https://developers.cloudflare.com/workers/local-development/

Loguru:
https://loguru.readthedocs.io/

Docker:
https://docs.docker.com/

Ruff:
https://docs.astral.sh/ruff/

ty:
https://docs.astral.sh/ty/

Pydantic Evals:
https://ai.pydantic.dev/evals/

Cloudflare Python + D1:
https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/

Cloudflare Workers limits:
https://developers.cloudflare.com/workers/platform/limits/

Cloudflare D1 pricing:
https://developers.cloudflare.com/d1/platform/pricing/

Cloudflare Vectorize pricing:
https://developers.cloudflare.com/vectorize/platform/pricing/

Cloudflare Workers AI pricing:
https://developers.cloudflare.com/workers-ai/platform/pricing/

EmbeddingGemma 300M:
https://developers.cloudflare.com/workers-ai/models/embeddinggemma-300m/

GLM-4.7-Flash:
https://developers.cloudflare.com/workers-ai/models/glm-4.7-flash/

Telegram Bot API:
https://core.telegram.org/bots/api

Base de coneixement inicial:
https://bhc-fcb.netlify.app/
