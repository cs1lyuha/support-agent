# 🛟 Support Agent cu acțiuni controlate

Un agent de suport clienți pentru un magazin online fictiv (**TechNova**, Moldova). Agentul răspunde din documentația oficială, cu citări. Poate verifica o comandă, poate deschide un tichet și poate crea cereri de rambursare, dar **nu poate da bani înapoi fără aprobarea unui om**. Orice acțiune trece prin autorizare și prin guardrails și ajunge într-un audit log care nu poate fi modificat fără să se observe.

> Proiectul 1 — demonstrează **RAG, tools, auth, state, guardrails, evaluare și business thinking**.

![Chat client](docs/screenshots/chat.png)

| Consola operatorului (aprobare rambursări) | Audit log cu lanț de hash-uri |
|---|---|
| ![Operator](docs/screenshots/operator.png) | ![Audit](docs/screenshots/audit.png) |

---

## Ce face

| Funcție | Cum e implementată | Unde |
|---|---|---|
| **RAG din documentație** | Documentele `kb/*.md` sunt împărțite pe secțiuni. Căutarea e BM25 cu normalizare pentru română (fără diacritice, stemming prin prefix), plus sinonime de domeniu și un prag de relevanță. | `support_agent/rag.py` |
| **Citări** | Fiecare secțiune are un `source_id` stabil (`livrare#costul-livrarii`). Agentul trebuie să-l citeze, iar codul **șterge citările inventate**, adică cele care nu au fost recuperate în tura curentă. | `guardrails.check_output` |
| **Verificare comandă** | Unealta `get_order` arată statusul, AWB-ul, eligibilitatea pentru rambursare și suma maximă rambursabilă. Clientul vede **doar comenzile lui**. | `tools.py` |
| **Creare tichet** | Unealta `create_ticket` cu categorie și prioritate. | `tools.py` |
| **Refund cu aprobare umană** | Unealta `request_refund` doar **creează o cerere** `pending`. Politica (30 de zile, status, produse nereturnabile, sumă maximă, dubluri, limită pe sesiune) e verificată **în cod**. Banii pleacă numai după ce operatorul apasă „Aprobă”. | `guardrails.check_refund`, `agent.decide_refund` |
| **Audit log** | Jurnal append-only cu **lanț SHA-256**: modificarea oricărui rând se detectează. Datele personale (card, e-mail, telefon) sunt mascate. | `audit.py` |
| **Fallback la om** | Conversația trece la un operator în 5 cazuri: clientul cere un om, 2 întrebări la rând fără răspuns în documentație, încercări repetate de prompt injection, eroare a modelului sau refuz de siguranță. Operatorul răspunde din consolă. | `agent.py` |
| **Auth** | Token Bearer cu roluri `customer` / `operator`. Clientul nu poate citi sesiunea altui client și nu poate apela endpoint-urile de operator. | `api.py` |
| **State** | Mașină de stări pe sesiune: `active → awaiting_approval → active`, `→ handoff → active`, `closed`. Istoricul conversației cu modelul e persistat în SQLite. | `agent.py`, `db.py` |
| **Guardrails** | Pe intrare: injecție, card (Luhn), cerere de om. Pe acțiuni: politica de refund. Pe ieșire: citări false, promisiuni false („rambursarea a fost aprobată”). | `guardrails.py` |
| **Evaluare** | 21 de conversații scriptate, verificate pe unelte, citări, stare, guardrails și DB, plus retrieval (hit@k, MRR, respingerea întrebărilor din afara documentației). | `eval/` |
| **Business thinking** | Metrici în consolă: containment rate, escaladări, sume rambursate sau blocate, cost LLM per conversație. | `api.business_metrics` |

## Arhitectură

```mermaid
flowchart LR
    C[Client<br/>chat web / CLI] -->|Bearer token| API[FastAPI<br/>auth + roluri]
    API --> G1{Guardrails intrare<br/>card · injecție · cere om}
    G1 -->|blocat| R[Răspuns sigur + audit]
    G1 --> B[Creier<br/>Claude tool use<br/>sau offline determinist]
    B -->|tool call| T[Unelte<br/>search_kb · get_order · create_ticket<br/>request_refund · escalate_to_human]
    T --> P{Politică în cod<br/>ownership · 30 zile · sume}
    P --> DB[(SQLite<br/>orders · refunds · tickets<br/>sessions · audit)]
    T --> KB[(kb/*.md<br/>BM25)]
    B --> G2{Guardrails ieșire<br/>citări reale · fără promisiuni false}
    G2 --> C
    O[Operator uman] -->|aprobă / respinge / preia| API
```

**Principiul de bază:** modelul *propune*, codul *decide*. Regulile de business nu stau doar în prompt, unde un client creativ le-ar putea ocoli. Ele stau în `guardrails.py` și se aplică indiferent ce scrie modelul. Testul `test_model_cannot_bypass_refund_policy` arată asta: un model care încearcă să ramburseze o comandă veche de 45 de zile este blocat.

## Pornire rapidă

```bash
git clone https://github.com/cs1lyuha/support-agent.git
cd support-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m support_agent.cli serve          # http://127.0.0.1:8000  (chat)  și  /operator  (consolă)
```

Fără cheie API, agentul rulează în **modul offline**: un router determinist de intenții care folosește *aceleași* unelte și guardrails. E bun pentru demo, pentru teste și ca bază de comparație.

### Cu Claude (agentul real)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m support_agent.cli serve --brain claude
```

Modelul implicit este `claude-opus-5-5`, cu tool use strict, adaptive thinking, prompt caching și fallback server-side la refuz. Se configurează prin `SUPPORT_MODEL` și `SUPPORT_EFFORT` (`low`/`medium`/`high`).

### Chat în terminal

```bash
python -m support_agent.cli chat --user tok_ana        # sau tok_ion
```

### Conturi demo

| Token | Rol | Comenzi |
|---|---|---|
| `tok_ana` | client (Ana) | A1001 livrată acum 5 zile · A1002 livrată acum 45 de zile · A1003 în procesare |
| `tok_ion` | client (Ion) | B2001 expediată · B2002 card cadou · B2003 laptop + încărcător |
| `tok_operator` | operator | aprobă rambursări, preia escaladări, vede auditul |

## Evaluare

```bash
pytest -q                                  # 19 teste: guardrails, audit, auth, flux aprobare, bucla Claude (client fals)
python -m eval.run_eval                    # offline, gratuit
python -m eval.run_eval --brain claude     # agentul real; afișează tokenii și costul
```

Rezultatul curent (offline):

```
== Retrieval: hit@1=0.957 hit@3=0.957 MRR=0.957 out-of-scope respinse=1.0
   ✗ „În cât timp ajunge coletul?” → găsește „colet deteriorat” în loc de „termene de livrare”
== 21/21 cazuri trecute (100%)
   pe categorii: auth 2/2 · fallback 3/3 · guardrail 3/3 · rag 3/3 · refund 7/7 · state 1/1 · tools 2/2
```

Eșecul de retrieval e lăsat intenționat vizibil. Căutarea lexicală nu leagă „ajunge” de „termene de livrare” fără un sinonim adăugat special pentru acest test, iar asta ar fi supra-ajustare. Soluția corectă e căutarea hibridă (BM25 + embeddings), descrisă mai jos.

**Onest despre evaluare:** setul are 26 de întrebări și 21 de conversații, iar sinonimele au fost ajustate pe el. Pe date reale ar trebui un set separat (held-out) din conversații reale anonimizate. Cazurile acoperă special situațiile cu risc (bani, date personale, autorizare), nu doar răspunsurile „frumoase”.

## Business thinking

- **KPI principal: containment rate**, adică ponderea conversațiilor rezolvate fără om. Un agent care rezolvă 100% e suspect: ori nu escaladează când ar trebui, ori aprobă lucruri pe care n-ar trebui. De aceea măsurăm și escaladările, și refund-urile blocate de politică.
- **Riscul financiar e limitat prin design.** Agentul nu poate muta bani. În cel mai rău caz, creează o cerere pe care un om o respinge. Tot ce nu se poate anula trece prin om (human-in-the-loop). Tot ce se poate anula și e ieftin (tichete, informații) e automat.
- **Cost:** fiecare apel LLM e înregistrat în tabela `usage`, iar consola arată costul total. Promptul de sistem și uneltele sunt stabile, deci intră în prompt cache. Eval-ul raportează costul per rulare, ca să compari modele sau niveluri de `effort`.
- **Încredere:** citările permit clientului și operatorului să verifice sursa fiecărui răspuns. Auditul cu hash-uri ajută la dispute („agentul mi-a promis…”) și la conformitate.
- **Degradare elegantă:** dacă API-ul pică, clientul nu primește o eroare. Conversația ajunge la un om.

## Limite și pași următori

- Căutare hibridă (BM25 + embeddings) pentru întrebări parafrazate.
- Detectorul de injecție e bazat pe reguli. În producție s-ar adăuga un clasificator și limitarea ratei de cereri.
- Autentificarea e cu token-uri demo. În producție: OAuth/SSO și token-uri cu expirare.
- SQLite e suficient pentru demo. Pentru mai multe instanțe: Postgres și o coadă pentru aprobări.
- Notificări către operator (Slack sau e-mail) la rambursări noi și escaladări.

## Structura

```
support_agent/
  api.py         FastAPI: auth, endpoint-uri client/operator, metrici
  agent.py       orchestrator + mașina de stări + acțiuni operator
  brains.py      ClaudeBrain (tool use) și OfflineBrain (determinist)
  tools.py       unelte cu autorizare + audit
  guardrails.py  injecție, PII, politica de refund, verificarea ieșirii
  rag.py         chunking + BM25 + citări
  audit.py       audit log cu lanț de hash-uri
  db.py          schema SQLite + date demo
  cli.py         serve / chat / reset
  static/        UI client + consolă operator
kb/              documentația (sursa pentru RAG)
eval/            cazuri de evaluare + runner
tests/           pytest
```

Explicația pas cu pas și un scenariu de demo sunt în **[CUM-FUNCTIONEAZA.md](CUM-FUNCTIONEAZA.md)**.
