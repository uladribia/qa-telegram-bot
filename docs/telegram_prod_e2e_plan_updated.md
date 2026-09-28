# Automated production Telegram E2E with Telethon — updated plan

**Repository reviewed:** `uladribia/qa-telegram-bot`
**Main reviewed at:** `fddc07e0eb4b68b0b94818029cafaff2cc3e5f36`
**Relevant latest implementation commit:** `a59473d2ef938cb5c3bf2e72a4530e0d77bdb637`

This supersedes the previous Telethon implementation plan.

---

## 0. What changed since the previous plan

The latest commits added much stronger offline flow coverage and fixed three real defects:

1. re-flagging a resolved answer inside the same second no longer collides on the feedback id;
2. group variants in the review report resolve their logical space title correctly;
3. `/reviewer` behavior in unregistered groups is explicitly pinned.

The integration tier now also covers:

- local and global reviewer nomination;
- replacing and removing reviewers;
- routing local reviewer → global reviewer → admin;
- reject → re-flag → approve;
- forged global approval denial;
- stranger denial;
- unreachable reviewer escalation;
- global correction over a local override;
- failed projection after approval;
- successful revert + reprojection.

This changes the purpose of the real-Telegram suite:

> **Offline integration tests own business invariants and adversarial authorization.
> Telethon owns the real external chain: Telegram MTProto → Bot API webhook → deployed Worker → D1/Vectorize/Workers AI → Telegram delivery.**

We still exercise every important normal user-facing flow through real Telegram, but we do not manufacture extra Telegram identities or deliberately break production dependencies to duplicate deterministic integration tests.

There is currently **no `e2e/` directory and no Telethon dependency group on `main`**, so the Telethon work is still to be implemented.

---

# 1. Objective

Add one explicit command that runs a stateful black-box E2E scenario against the deployed bot using the **existing human Telegram account**.

No second SIM, eSIM, phone number, Telegram account, Playwright, browser automation, or MCP browser control.

Real path under test:

```text
existing Telegram user via Telethon
  -> real Telegram
  -> Telegram Bot API
  -> POST /adapters/telegram/webhook
  -> deployed Cloudflare Worker
  -> D1 + Vectorize + Workers AI
  -> Telegram Bot API outbound
  -> real Telegram
  -> Telethon assertions
```

This is **not** a golden/eval suite.

Do not call `evals/`, `make eval-live*`, or the manual 57-question checklist.

---

# 2. Hard constraints

The implementation agent must obey these literally.

1. No Telethon import below `src/knowledge_bot/`.
2. No Telethon default dependency.
3. No Telethon in the Worker/Docker production dependency set.
4. No second Telegram account.
5. No browser automation.
6. No special production E2E mode.
7. No E2E-only production route.
8. No authorization bypass.
9. No direct webhook injection from the E2E actor.
10. Normal user actions enter through real Telegram.
11. Internal HTTP endpoints may be used only for deterministic fixture setup/cleanup and the daily-report operator flow.
12. Real Telegram E2E does not run from ordinary CI, `make all`, `make test`, `make test-integration`, `make test-all`, or `make deploy`.
13. Require `ALLOW_CLOUDFLARE_LIVE_TESTS=1`.
14. Use two Telegram groups whose configured titles begin exactly with `[E2E]`.
15. Never mutate reviewer state outside those two dedicated E2E groups.
16. Do not mutate the global reviewer configuration.
17. Run sequentially; no parallel E2E executions.

---

# 3. Optional dependency group

Modify `pyproject.toml`.

Add a new non-default dependency group, analogous to the existing `local` group:

```toml
[dependency-groups]
# existing dev/local groups stay unchanged

e2e = [
    "httpx>=0.28.1",
    "telethon>=1,<2",
]
```

Do **not** add `e2e` to:

```toml
[tool.uv]
default-groups = ["dev"]
```

Update `uv.lock`.

Required behavior:

```bash
uv sync --frozen
```

must not install Telethon.

This must install it:

```bash
uv sync --group e2e
```

Add:

```toml
"e2e/**/*.py",
```

to Ruff's include list.

Do not add `e2e` to default `tool.ty.src.include`, because the default environment intentionally does not contain Telethon.

The E2E Make target type-checks the package with `--group e2e`.

---

# 4. Files

Add:

```text
e2e/
  __init__.py
  telegram/
    __init__.py
    config.py
    client.py
    bootstrap.py
    run.py

.env.e2e.example
```

Edit:

```text
.gitignore
pyproject.toml
uv.lock
Makefile
README.md
docs/e2e-telegram.md
docs/development.md
AGENTS.md
```

Do **not** put the live tests under `tests/`.

Reason: `pytest` owns deterministic offline tests. `make test-all` must never accidentally discover a real network test requiring Telethon credentials.

---

# 5. Local secrets/config

## `.gitignore`

The repository already ignores `.env.*`, so explicitly allow the example file.

Add:

```gitignore
# Real Telegram E2E
.e2e/
.env.e2e
!.env.e2e.example
```

The `.e2e/` directory contains the Telethon user session and is credential material.

## `.env.e2e.example`

Create:

```dotenv
ALLOW_CLOUDFLARE_LIVE_TESTS=0

BOT_BASE_URL=
INTERNAL_ADMIN_KEY=

# Existing HUMAN Telegram account, not the bot.
TELEGRAM_E2E_API_ID=
TELEGRAM_E2E_API_HASH=

TELEGRAM_E2E_BOT_USERNAME=

# Dedicated test groups containing this human account and the bot.
TELEGRAM_E2E_GROUP_A_TITLE=[E2E] QA A
TELEGRAM_E2E_GROUP_B_TITLE=[E2E] QA B

TELEGRAM_E2E_SESSION_PATH=.e2e/telegram-user
TELEGRAM_E2E_TIMEOUT_SECONDS=75
```

Do not put these settings into `knowledge_bot.infrastructure.settings.Settings`.

In `e2e/telegram/config.py`, create a dedicated `TelegramE2ESettings` using `pydantic-settings` and `.env.e2e`.

Validate before connecting or mutating anything:

- live flag is `1`;
- `BOT_BASE_URL` is non-empty HTTPS;
- internal key exists;
- Telegram API id/hash exist;
- bot username exists;
- A and B titles differ;
- both titles start with `[E2E]`;
- timeout > 0.

---

# 6. One-time existing-account login

Implement:

```bash
make telegram-e2e-login
```

`e2e/telegram/bootstrap.py` must:

1. load `.env.e2e`;
2. create the parent directory for the session;
3. instantiate Telethon with the existing human account's API id/hash;
4. perform Telethon's normal interactive login;
5. allow Telegram to request the existing phone number, login code and 2FA password;
6. persist the `.session` file below `.e2e/`;
7. call `get_me()`;
8. print only display name, username and numeric Telegram user id;
9. never print the API hash, auth key, login code or password.

This is the only interactive step.

Subsequent E2E runs are headless.

Use a normal file session, not `StringSession`.

---

# 7. Telethon client helper

Implement `e2e/telegram/client.py`.

Keep this deliberately small.

Required operations:

```python
connect()
close()

resolve_bot(username)
resolve_group_by_exact_title(title)

send(chat, text, *, reply_to=None) -> Message

wait_for_bot_message(
    chat,
    *,
    after_id,
    predicate=None,
    timeout=None,
) -> Message

assert_no_bot_message(
    chat,
    *,
    after_id,
    seconds,
) -> None

click_button(message, text)
reply(message, text) -> Message
```

Rules:

- cache the bot entity/id;
- resolve groups from dialogs by **exact title**;
- derive the Bot-API-compatible signed chat id with Telethon's peer-id utility;
- only match bot messages:
  - in the expected chat;
  - from the expected bot id;
  - newer than the saved `after_id`;
  - satisfying the optional predicate;
- never accidentally accept a message from an earlier run;
- polling recent messages every ~0.5–1 s is fine;
- use bounded timeouts;
- button lookup is by visible label, not index.

Current visible labels to use:

```text
⚠️ Està malament?
✏️ Editar
❌ Rebutjar
👥 Aprovar grup
🌐 Aprovar global
```

Do not expose callback payloads to `run.py`.

The whole point is to press the real Telegram buttons.

---

# 8. Runner shape

Implement `e2e/telegram/run.py`.

Use:

```python
asyncio.run(main())
```

This is one sequential stateful scenario, not pytest.

Define a simple explicit exception:

```python
class E2EFailure(RuntimeError):
    pass
```

Print progress:

```text
[01/12] preflight ................................ ok
[02/12] mention, /ask, reply and DM .............. ok
...
```

Exit non-zero at the first test failure after cleanup has been attempted.

Cleanup belongs in `finally`.

If cleanup also fails, print the cleanup error but preserve the original test failure.

---

# 9. Internal HTTP client

Use `httpx.AsyncClient`.

Header:

```python
{"X-Internal-Key": settings.internal_admin_key}
```

Only use existing routes:

```text
POST /internal/groups
POST /internal/seed
POST /internal/revert
POST /internal/jobs/daily-report
```

Do not add a new route.

Do not call:

```text
/internal/eval/*
/internal/retrieve
/internal/reindex
```

The E2E command must never invoke the golden/frozen eval machinery.

---

# 10. Dedicated spaces and fixture

Use stable logical spaces:

```python
SPACE_A = "sp_000000000000000000000000000000a1"
SPACE_B = "sp_000000000000000000000000000000b2"

SCOPE_A = f"space:{SPACE_A}"
SCOPE_B = f"space:{SPACE_B}"
```

Bind the exact dedicated groups on every run using `/internal/groups`.

Example:

```json
{
  "chat_id": "<Telethon-derived signed peer id>",
  "title": "[E2E] QA A",
  "space_id": "sp_000000000000000000000000000000a1"
}
```

Never bind a chat whose resolved title does not start with `[E2E]`.

## Sentinel Q&A

Use one fixed canonical question:

```python
SENTINEL_QUESTION = "Quin és el codi de la prova E2E de Telegram?"
BASELINE_TOKEN = "E2E-BASELINE-42"
BASELINE_ANSWER = "El codi de la prova E2E de Telegram és E2E-BASELINE-42."
SOURCE_URL = "https://e2e.invalid/telegram"
SOURCE_ANCHOR = "telegram-e2e-sentinel"
```

Generate a short random `run_id` per run:

```text
LOCAL-<run_id>
GLOBAL-<run_id>
LISTENER-<run_id>
REJECT-<run_id>
```

Seed the fixed baseline in:

```text
global
space:<SPACE_A>
```

Do **not** seed a local B baseline.

Reason:

- A needs a local base so a local correction can later be reverted cleanly.
- B should inherit global so it proves global propagation.

Use the repository's own pure helpers from the E2E package to derive the E2E QA item ids:

```python
canonical_key_for
stable_id
```

Do not copy their algorithms.

---

# 11. Pre-run reset

The latest repo now explicitly tests revert + reprojection offline, so the live runner may safely use the existing revert route as fixture cleanup.

Before seeding:

1. derive the dedicated global sentinel QA item id;
2. derive the dedicated A-local sentinel QA item id;
3. call `/internal/revert` repeatedly for each, maximum 10 times;
4. `404` means "nothing more to revert" and is success;
5. seed the fixed baseline globally with `renew=true`;
6. seed the fixed baseline into `SCOPE_A` with `renew=true`.

Never revert any non-E2E item.

Do not add delete endpoints.

---

# 12. Critical routing update from the latest commits

The current code now explicitly supports:

```text
local reviewer
 -> global reviewer
 -> admin
```

Therefore the live test must **not** assume that a B correction falls back to the admin. Production may already have a global reviewer.

To keep this black-box run deterministic without touching global configuration:

> **Nominate the existing human/admin account as the local reviewer in BOTH E2E group A and E2E group B.**

This forces review delivery for both groups to the Telethon actor independently of any existing global reviewer.

Because the same actor is also the configured admin, the review still exposes both local and global approval buttons.

Never set, replace or remove a global reviewer in the live E2E harness.

---

# 13. Exact live scenario

## Step 1 — Preflight

1. require live opt-in;
2. connect the existing Telethon session;
3. assert `get_me()` is a human account;
4. resolve the configured bot and assert it is a bot;
5. resolve exact E2E groups A and B;
6. derive signed chat ids;
7. register both groups into their fixed spaces;
8. send `/start` in the bot DM so Telegram has an open private channel;
9. wait for any new bot DM response;
10. reset + seed the sentinel baseline.

If reviewer nomination later produces no admin confirmation, fail with:

```text
The Telethon user is not the deployed ADMIN_TELEGRAM_USER_ID.
Use the existing admin Telegram account for this suite.
```

---

## Step 2 — Addressing flows

### Mention

In A:

```text
@<bot_username> Quin és el codi de la prova E2E de Telegram?
```

Assert:

- response contains `E2E-BASELINE-42`;
- response contains `Fonts:`;
- response has `⚠️ Està malament?`.

Save this bot answer message: it is reused later for the reject/re-flag regression scenario.

### `/ask`

In B:

```text
/ask Quin és el codi de la prova E2E de Telegram?
```

Assert it contains `E2E-BASELINE-42`.

### Reply-to-bot

Reply in A to the bot's mention answer with the plain question, no mention and no command:

```text
Quin és el codi de la prova E2E de Telegram?
```

Assert baseline token.

### DM

Send the plain sentinel question to the bot DM.

Assert baseline token.

This covers all current addressing mechanisms with real Telegram.

---

## Step 3 — Abstention

In B send:

```text
@<bot_username> Quin és el planeta secret E2E-UNKNOWN-<run_id>?
```

Assert the bot response is:

```text
No tinc prou informació fiable per respondre-ho.
```

One case only.

Do not run the golden suite.

Do not intentionally break Workers AI to produce `unavailable`.

---

## Step 4 — Real background listener + explicit reply pairing

In A send an **unaddressed human** question:

```text
Prova LISTENER-<run_id>: a quina hora és l'activitat?
```

No mention, `/ask`, or reply to bot.

Record the newest message id before sending and assert that the bot sends nothing for a short quiet window.

Allow enough time for deferred webhook processing/classification.

Reply as the same human account to that human question:

```text
L'activitat de la prova LISTENER-<run_id> és a les 17:42.
```

Again, do not address the bot.

Wait a bounded period for classification, pairing and projection.

Then ask:

```text
@<bot_username> A quina hora és l'activitat de la prova LISTENER-<run_id>?
```

Assert the response contains:

```text
17:42
```

and preferably `LISTENER-<run_id>` if the generator preserves it.

This proves through real production:

```text
group chatter
 -> listener
 -> classifier
 -> durable message
 -> explicit reply pairing
 -> projection
 -> later retrieval
 -> generated Telegram response
```

Do not inspect D1 to decide whether the step passed.

---

## Step 5 — Local reviewer setup in BOTH groups

Use one existing human message in A.

Reply to it as admin:

```text
/reviewer
```

Assert group confirmation contains:

```text
revisor d'aquest grup
```

Repeat in B.

Then send plain:

```text
/reviewer
```

in A and assert the bot returns a list containing:

```text
Revisors:
```

Do not assert the complete list because production may contain other reviewers.

Do not touch `/reviewer global`.

---

## Step 6 — Reject then re-flag the SAME answer

This is intentionally updated to exercise the exact regression fixed in the latest commit.

Use the saved **same bot answer message** from Step 2.

### First correction: reject

Click on that saved answer:

```text
⚠️ Està malament?
```

In bot DM wait for the force-reply prompt containing:

```text
Què corregiries?
```

Reply to that exact prompt:

```text
Proposta REJECT-<run_id>.
```

Wait for:

- reporter acknowledgement containing `Ho he enviat a revisió`;
- review message containing `REJECT-<run_id>`.

Click:

```text
❌ Rebutjar
```

Wait for:

```text
❌ Correcció rebutjada.
```

### Re-flag the exact same original answer

Immediately click `⚠️ Està malament?` again on the **same original bot answer message**.

This must create a fresh correction and must not error/collide.

Wait for a new force-reply prompt newer than the previous one.

Reply:

```text
Esborrany LOCAL-<run_id>.
```

Wait for a new review containing that token.

This real-Telegram sequence complements the new offline
`test_two_flags_in_the_same_second_do_not_collide` without trying to control the production clock.

---

## Step 7 — Edit + local approval

On the second review from Step 6 click:

```text
✏️ Editar
```

Wait for:

```text
Envia'm el text correcte.
```

Reply:

```text
El codi E2E local és LOCAL-<run_id>.
```

Wait for the regenerated review containing `LOCAL-<run_id>`.

Click:

```text
👥 Aprovar grup
```

Wait for an approval confirmation.

Ask the sentinel in A:

```text
@<bot_username> Quin és el codi de la prova E2E de Telegram?
```

Assert:

```text
LOCAL-<run_id>
```

Ask sentinel in B.

Assert:

```text
E2E-BASELINE-42
```

This proves real D1/Vectorize scope isolation.

---

## Step 8 — Global correction from B

Because B has its own local self-reviewer, review delivery is deterministic even if production has a separate global reviewer.

Ask sentinel in B and obtain the baseline/global answer.

Click `⚠️ Està malament?`.

Propose:

```text
El codi E2E global és GLOBAL-<run_id>.
```

Wait for the review in the bot DM.

Click:

```text
🌐 Aprovar global
```

Wait for approval confirmation.

Now ask:

### B

Assert:

```text
GLOBAL-<run_id>
```

### A

Assert:

```text
LOCAL-<run_id>
```

This is the real external equivalent of the latest
`test_global_correction_leaves_the_local_variant_answering`.

---

## Step 9 — Daily report Telegram delivery

This step is allowed only because this repository is currently deployed as a test bot/Worker.

Record the newest bot DM id.

Call:

```http
POST /internal/jobs/daily-report
X-Internal-Key: ...
Content-Type: application/json

{"force": true}
```

Require HTTP success and body status `sent`.

Wait for a new DM starting:

```text
📊 Resum diari
```

Assert only stable section labels:

```text
Preguntes adreçades:
Preguntes de fons:
Correccions:
IA:
```

Do not assert counts.

**If the deployment later becomes a real user-facing production bot, move this step behind a separate explicit flag or omit it**, because `force=true` advances daily-report state.

---

# 14. Things deliberately NOT reproduced live

The latest commits now cover these strongly offline.

Do not add complexity or extra accounts to reproduce them over real Telegram:

- non-admin reviewer nomination;
- stranger confirmation;
- forged local-reviewer global approval;
- local reviewer permission semantics with a distinct identity;
- replacing one human reviewer with another;
- unreachable reporter/reviewer DM;
- escalation timeout;
- failed projection after approval;
- invalid webhook secret;
- duplicate Telegram update idempotency;
- forced Workers AI outage;
- review-report group-title rendering;
- successful revert semantics/reprojection.

The live harness does use `/internal/revert` for its own dedicated cleanup, but its correctness is already pinned offline.

This is not a reduction in coverage: it is a clean split between deterministic business tests and real external-boundary tests.

---

# 15. Cleanup

Run in `finally`, even after failure.

1. In A send:

```text
/reviewer off
```

2. In B send:

```text
/reviewer off
```

3. Revert the dedicated A-local sentinel item repeatedly until `404`, max 10.
4. Revert the dedicated global sentinel item repeatedly until `404`, max 10.
5. Do not modify global reviewer state.
6. Do not delete Telegram messages.
7. Do not add generic delete endpoints.
8. Close Telethon.

Listener messages, feedback history and old Q&A versions may remain in this dedicated test environment. Their unique `run_id` makes them harmless and useful for debugging.

If reviewer cleanup fails, report it loudly because stale local E2E reviewer state can change future runs.

---

# 16. Make targets

Add:

```make
.PHONY: telegram-e2e-login test-e2e-telegram

telegram-e2e-login:
	uv run --group e2e python -m e2e.telegram.bootstrap

test-e2e-telegram:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || \
		(echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	uv run --group e2e ty check e2e
	uv run --group e2e python -m e2e.telegram.run
```

Do not chain this target from anything else.

Recommended operator sequence:

```bash
make lint
make test-integration

# one time
make telegram-e2e-login

# after a relevant deploy
ALLOW_CLOUDFLARE_LIVE_TESTS=1 make test-e2e-telegram
```

Do not make `test-e2e-telegram` automatically run `make test-integration`.
Keep the cheap deterministic gate and the stateful external gate independently callable.

---

# 17. Documentation update

## `docs/e2e-telegram.md`

The current document still says real Telegram acceptance is manual.

Rewrite it so:

- the primary deployed acceptance gate is the Telethon command;
- it explains the existing-account session;
- no second SIM/account is required;
- two dedicated `[E2E]` groups are required;
- offline integration owns authorization/business invariants;
- Telethon owns real delivery/Worker/D1/Vectorize/AI wiring;
- the old manual procedure remains only as troubleshooting/reference.

Do not imply the 57-question manual checklist runs as part of E2E.

## `docs/development.md`

Add the external tier explicitly:

```text
real Telegram E2E — on demand, networked, stateful, not pytest/CI
```

Keep the new `RepositorySearchIndexSource` / `FrozenClock` documentation added by the latest commit.

## `README.md`

Add commands:

```text
make telegram-e2e-login
make test-e2e-telegram
```

## `AGENTS.md`

Add:

> Telegram flow changes must pass `make test-integration` first. Run the real Telethon E2E only with explicit live authorization and dedicated `[E2E]` groups. The live suite complements, never replaces, deterministic authorization tests.

---

# 18. Verification

The implementation agent must run these in this order.

## A. Prove Telethon is optional

From a clean environment:

```bash
rm -rf .venv
uv sync --frozen
uv run python -c "import importlib.util; assert importlib.util.find_spec('telethon') is None"
```

Must pass.

## B. Existing deterministic gates

```bash
make lint
make test
make test-integration
```

Must pass without Telegram/Cloudflare network traffic.

## C. Optional group

```bash
uv sync --group e2e
uv run --group e2e python -c "import telethon; print(telethon.__version__)"
uv run --group e2e ty check e2e
```

Must pass.

## D. Runtime bundle isolation

Because dependency metadata changed:

```bash
make smoke
```

Confirm no Telethon import exists below `src/knowledge_bot/` and no production code depends on it.

## E. One-time login

```bash
make telegram-e2e-login
```

Confirm the generated Telethon `.session` is under `.e2e/` and ignored by Git.

## F. Real run

```bash
ALLOW_CLOUDFLARE_LIVE_TESTS=1 make test-e2e-telegram
```

Expected:

```text
PASS: real Telegram E2E
```

---

# 19. Acceptance criteria

- [ ] `main` still has no production Telethon dependency.
- [ ] default `uv sync --frozen` does not install Telethon.
- [ ] `uv sync --group e2e` installs Telethon.
- [ ] Telethon exists only in the external E2E package.
- [ ] no extra Telegram account/phone/eSIM is needed.
- [ ] session credentials are gitignored.
- [ ] run refuses non-`[E2E]` group titles.
- [ ] run refuses without live opt-in.
- [ ] A/B use stable logical spaces.
- [ ] baseline reset is idempotent.
- [ ] mention path passes.
- [ ] `/ask` path passes.
- [ ] reply-to-bot path passes.
- [ ] DM path passes.
- [ ] one real abstention passes.
- [ ] listener stores an unaddressed question without replying.
- [ ] explicit human reply becomes retrievable paired evidence.
- [ ] local self-reviewer is configured in A.
- [ ] local self-reviewer is configured in B.
- [ ] reviewer list command works.
- [ ] correction force-reply DM works.
- [ ] rejection works.
- [ ] the same original answer can immediately be re-flagged.
- [ ] reviewer edit works.
- [ ] local approval works.
- [ ] A local override does not change B.
- [ ] B global approval works without relying on global-reviewer routing.
- [ ] A local override survives the later global correction.
- [ ] daily report reaches Telegram when that optional stateful step is enabled.
- [ ] cleanup removes both E2E local reviewers.
- [ ] cleanup reverts only the two dedicated sentinel items.
- [ ] ordinary CI remains offline.
- [ ] no golden/frozen eval is invoked.

---

# 20. Implementation principle

Do not make the product aware of the test harness.

The final workflow should remain:

```bash
# once
make telegram-e2e-login

# whenever the deployed Telegram stack must be validated
ALLOW_CLOUDFLARE_LIVE_TESTS=1 make test-e2e-telegram
```

The latest offline suite already proves the hard state-machine and permission behavior.

This command proves that the deployed system actually works when a real Telegram user uses it.
