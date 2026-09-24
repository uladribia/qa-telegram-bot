# Manual test questions

This checklist is derived from the committed Q&A seed material:

- `data/seed/qa.json` — club knowledge
- `data/seed/bot_self_qa.json` — bot self-knowledge

Use an explicit address in Telegram: mention the bot, reply to the bot, send a DM, or use `/ask`. For every positive case, verify the answer, the `Fonts:` section, and the expected source anchor. For `in_review` entries, the expected result is abstention because they are not active knowledge.

## Club knowledge

### Registration and payments

| # | Question | Expected result |
|---:|---|---|
| 1 | Què he de fer per inscriure o renovar un jugador/a? | Cluber, mobile login, personal data, section fee and payment method; source `qa-inscripcio-renovacio`. |
| 2 | Com funciona el procés d'inscripcions? | **Abstention expected**: entry is `in_review`; it must not be served as active knowledge. |
| 3 | Què inclou el pagament de l'inscripció? | Club fee plus annual federation licence and medical insurance; equipment is separate; source `qa-que-inclou-pagament`. |

### Organisation

| # | Question | Expected result |
|---:|---|---|
| 4 | Com està organitzada l'escola? | Categories from Escoleta to Juvenil, with Benjamí gender split where there are enough players; source `qa-organitzacio-escola`. |
| 5 | Quines edats corresponen a cada categoria? | School-year table for Minis, Prebenjamí, Benjamí, Aleví, Infantil, Cadet, Juvenil, Sénior, Papis/Mamis and Veterans; source `qa-taula-categories-edats`. |
| 6 | Quines seccions són del Barcelona Hockey Club (BHC) només i quines són també del FC Barcelona (FCB)? | Distinction between BHC-only and FCB categories; source `qa-bhc-vs-fcb`. |
| 7 | És veritat que algunes categories combinen entrenaments d'hoquei sala i d'hoquei herba durant part de la temporada? | Yes, especially from Aleví; exact schedule comes from the coach/parent delegate; source `qa-hockey-sala-herba-combinat`. |
| 8 | Quins valors defensa el club? | Humilitat, Esforç, Ambició, Respecte and Treball en equip; source `qa-valors-club`. |

### Facilities

| # | Question | Expected result |
|---:|---|---|
| 9 | On entrenen i juguen els partits els equips del club? | Complex Esportiu Municipal Pau Negre, address, home/away distinction; source `qa-instalacions-pau-negre`. |
| 10 | Què passa si hi ha un concert o un altre esdeveniment a Montjuïc que afecta l'accés a les instal·lacions? | Distinguish a formal access restriction from extra crowd/traffic; source `qa-concerts-esdeveniments-acces`. |
| 11 | Si plou, s'entrena igualment? | Training normally continues, with indoor activities if conditions change; source `qa-entrenaments-pluja`. |

### Equipment

| # | Question | Expected result |
|---:|---|---|
| 12 | Quan arriben els equipaments? | **Abstention expected**: entry is `in_review`; do not serve the unconfirmed answer. |
| 13 | Com puc saber quines talles escollir dels equipaments del FCB? | Consult the official FC Barcelona Store size guide; source `qa-talles-equipament`. |
| 14 | On puc consultar les taules de mides de l'equipament del BHC (samarreta, pantaló/faldilla, mitges, dessuadora, softshell, impermeable)? | BHC/Thunder size tables, distinct from the official FCB kit; source `qa-guia-talles-roba-thunder`. |
| 15 | Quant costa l'equipament del BHC i on es demana? | Listed indicative prices and order email; source `qa-preus-comanda-roba-thunder`. |
| 16 | Per què les jugadores porten faldilla en lloc de pantaló, i qui la proporciona? | Nike does not provide skirts; Thunder Hockey supplies them; source `qa-faldilla-equipament`. |
| 17 | Com i quan s'ha de demanar l'equipament? | Usually announced at the start of the season; try sizes at training, then the delegate sends the order list; source `qa-equipament-com-demanar`. |

### Communication

| # | Question | Expected result |
|---:|---|---|
| 18 | Per què no m'han convidat al grup de WhatsApp de la meva secció si ja he inscrit el meu/va fill/a al Cluber? | The delegate adds families after the club receives the Cluber list; possible registration issue; contact delegate; source `qa-grup-whatsapp`. |
| 19 | A qui puc contactar si tinc un dubte que el delegat/da de la meva secció no em pot resoldre? | Club email and phone/WhatsApp; source `qa-contacte-club`. |
| 20 | Hi ha algun horari d'atenció presencial per a dubtes administratius? | Pau Negre office, Tuesdays and Thursdays 18:00–21:00; source `qa-despatx-atencio-pau-negre`. |

### Medical certificate

| # | Question | Expected result |
|---:|---|---|
| 21 | Qui i quan es fan les revisions mèdiques? | The club's medical provider at the start of the course, normally September; delegate coordinates; source `qa-revisions-mediques`. |
| 22 | Un cop feta la revisió mèdica, he d'enviar al delegat/da l'informe que m'han donat? | No; the provider sends results to the delegate/club and uploads them to the federation; source `qa-informe-revisio-medica`. |
| 23 | Té algun cost la revisió mèdica que proposa el club? | Usually about €20 and valid for two seasons; confirm the current price; source `qa-revisio-medica-cost`. |
| 24 | Han d'estar presents els pares/mares a la revisió mèdica? | Yes, for younger players; source `qa-acompanyament-revisio-medica`. |
| 25 | Es pot lliurar l'anamnesi en format digital, o cal portar-la impresa a la revisió mèdica? | It must be printed and brought to the appointment; source `qa-anamnesi-format`. |
| 26 | Per què és important el certificat mèdic? | It is mandatory and must be valid; without it the player cannot be fully registered; source `qa-certificat-medic`. |
| 27 | On puc descarregar l'app de la Federació Catalana d'Hoquei? | Official App Store and Google Play downloads; source `qa-app-federacio-descarrega`. |
| 28 | Com puc saber la data de caducitat del certificat mèdic per a la Federació? | Use the federation app: Esportista, DNI/date of birth, then account settings; source `qa-caducitat-certificat-medic`. |
| 29 | Cal apuntar-se o reservar hora pel meu compte per fer la revisió mèdica al Pau Negre? | No; the delegate collects names and coordinates the list; source `qa-revisio-medica-reserva`. |

### Bus, matches and events

| # | Question | Expected result |
|---:|---|---|
| 30 | Com funciona el servei d'autobús? | Optional seasonal service, route survey, sibling discounts, monitor, usually one way; source `qa-servei-autobus`. |
| 31 | Està contemplada la possibilitat d'utilitzar el servei de bus tan sols un dia a la setmana? | Two days get priority; one day may open if places remain; source `qa-bus-un-dia`. |
| 32 | Què és això dels BALL-KIDS? | Helpers at older teams' matches, with WhatsApp call, uniform and arrival time; source `qa-ball-kids`. |
| 33 | El club organitza algun casal d'estiu? | Yes, annual BHC summer camp with seasonal details; source `qa-casal-estiu-bhc`. |

### Minis

| # | Question | Expected result |
|---:|---|---|
| 34 | Per què els minis han de pagar la fitxa federativa? | Mandatory licence and medical-insurance safety coverage, with a trial period sometimes offered; source `qa-mini-fitxa-federativa`. |
| 35 | Els minis juguen a les competicions? | No own competition; occasional invitation to Prebenjamí may occur; source `qa-mini-competicions`. |
| 36 | Què passa si un mini només entrena un cop per setmana? | They may train once weekly; regular attendance improves group experience; source `qa-mini-un-cop-setmana`. |

## Bot self-knowledge

| # | Question | Expected result |
|---:|---|---|
| 37 | Qui ets? | Club knowledge bot, cited answers and abstention; source `usage.md`. |
| 38 | Què saps fer? | Answers from curated knowledge, cites sources, abstains and learns from approved corrections; source `usage.md`. |
| 39 | Com funciona el bot? | Address detection, retrieval, direct/synthesis/abstention and feedback button; source `usage.md`. |
| 40 | Per què no contestes missatges que no són per tu? | It answers only explicit mentions, replies, DMs and `/ask`; source `usage.md`. |
| 41 | Com puc saber d'on surt una resposta? | Every answer has a `Fonts:` section; source `usage.md`. |
| 42 | Com corregeixo una resposta equivocada? | Press the feedback button and reply in the private prompt; source `usage.md`. |
| 43 | Qui pot aprovar una correcció? | Local reviewer, global reviewer or admin, with scope restrictions; source `usage.md`. |
| 44 | Quan aprovo una correcció, es perd la resposta antiga? | No, approval creates a new version and keeps history; source `usage.md`. |
| 45 | Per què de vegades dius que no ho saps? | Abstention is intentional when evidence is insufficient; source `usage.md`. |
| 46 | Pots processar fotos o àudios? | No, attachments are metadata-only; captions are treated as text; source `usage.md`. |
| 47 | Quines llengües parles? | Responds in the language of the question; source `usage.md`. |
| 48 | Què passa quan t'esgoten els recursos d'IA? | Temporary unavailable response; source `operations.md`. |
| 49 | D'on treus la informació per respondre? | Corrections, official Q&A, group messages and listener evidence in authority order; source `knowledge-base.md`. |
| 50 | Aprens del que es parla al grup? | Stores background messages, indexes eligible evidence and does not answer unaddressed chatter; source `knowledge-base.md`. |
| 51 | Ets un assistent intel·ligent general? | No, it is a scoped club knowledge bot; source `usage.md`. |
| 52 | Qui és el revisor del meu grup? | The admin appoints reviewers through `/reviewer`; the bot cannot be nominated; source `usage.md`. |
| 53 | Com puc saber si el bot respon al meu grup? | The group must have a logical-space binding; there is no static group allowlist; source `usage.md`. |
| 54 | Es pot fer funcionar sense Cloudflare? | Yes, local Docker uses SQLite, NumPy and Ollama; source `setup-local.md`. |
| 55 | On es desen els vectors en local? | In the rebuildable `local_vectors` projection inside the Docker data volume; source `operations.md`. |
| 56 | Què passa si el revisor no pot rebre el missatge privat? | Review stays pending and escalates to admin after the timeout; source `operations.md`. |
| 57 | Com aparelles una pregunta i una resposta que no tenen resposta directa? | Conversation quiet-window extraction, validation and message evidence, never automatic canonical Q&A; source `knowledge-base.md`. |

## Local threshold calibration

The questions above were run against the local Docker models (`embeddinggemma` and `gemma3:270m`) after seeding the local Q&A material. The calibration used the expected active source as the positive target and `in_review` entries as negatives.

| Threshold | Club Q&A precision | Club Q&A recall | Decision |
|---:|---:|---:|---|
| 0.65 | 66.7% | 100% | Rejected: too many wrong top matches |
| **0.70** | **88.2%** | **83.3%** | **Selected: precision-first direct-answer gate** |
| 0.75 | 100% | 44.4% | Rejected: too many false abstentions |

The local configuration therefore keeps:

```dotenv
DIRECT_QA_THRESHOLD=0.70
SYNTHESIS_THRESHOLD=0.30
```

`SYNTHESIS_THRESHOLD` remains `0.30` because this question set validates direct-answer selection and abstention, not a separate labeled synthesis boundary. Lowering it would turn weak or mismatched matches into generated answers, which is unsafe for this bot. Re-run this calibration after changing the embedding model, seed content, or the retrieval projection.

## Retrieval and safety checks

After the direct questions, test these deliberately:

| Test | Question | Expected result |
|---:|---|---|
| R1 | Com es pot obtenir el resultat de la final de la Champions? | Abstention or no fabricated club answer. |
| R2 | Quina és la temperatura a Barcelona demà? | Abstention; no weather source is seeded. |
| R3 | Explica'm el codi de la despesa pública de demà. | Abstention; the bot is not a general assistant. |
| R4 | Quina és la mida de la samarreta BHC en talla 10? | Retrieve the seeded Thunder/BHC equipment size table, not the FCB Store table. |
| R5 | A quina hora s'entrena a Minis? | Abstention or a cautious answer only if WhatsApp/import evidence exists; the seed does not confirm a fixed schedule. |
| R6 | Quan arriba l'equipament? | Abstention because the seeded entry is `in_review`, not active knowledge. |

For each test, verify that the response never contains raw private data, fabricated citations, or a citation to an `in_review` entry.
