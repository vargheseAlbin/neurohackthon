# nuerohackthon

A [neuro-san-studio](https://github.com/cognizant-ai-lab/neuro-san-studio) project. Agent
networks are declared as HOCON files in `registries/`; the Python tools they call live in
`coded_tools/`.

## Running it

```cmd
uv sync
uv run ns run
```

That starts two processes:

| Process | Address |
| --- | --- |
| nsflow UI | http://localhost:4173 |
| neuro-san server | http://localhost:8080 |

Open the UI, pick a network from the sidebar, and chat with it. `Ctrl+C` stops both.

Useful flags: `--log-level debug`, `--nsflow-port <n>`, `--server-http-port <n>`,
`--server-only`, `--client-only`.

### LLM

Networks run on **Google AI Studio (Gemini)**. Put a key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) in `.env`:

```
GOOGLE_API_KEY=...
```

The model is set once, in [config/llm_config.hocon](config/llm_config.hocon)
(`gemini-3.5-flash`), and every network inherits it. Verify with `uv run ns check-config`.

> On a machine behind a TLS-inspecting proxy, Python's default certifi CA bundle does not
> contain the proxy's root CA and every HTTPS call — including the LLM calls — fails with
> `CERTIFICATE_VERIFY_FAILED`. `.env` therefore points `SSL_CERT_FILE` at
> `config/ca_bundle.pem`, which is certifi's bundle *plus* the OS-trusted roots. See the
> comment in `.env` for how to regenerate it.

### Without the UI

```cmd
uv run ns chat --list                :: what's available
uv run ns chat finance_agent         :: interactive, in-process, no server needed
uv run ns validate registries/finance_agent.hocon
uv run ns check-llm-keys
```

## Agent networks

| Network | What it does |
| --- | --- |
| `finance_agent` | Personal finance coach. See below. |
| `basic/music_nerd` | Single-agent hello world. |
| `basic/weather_agent` | Single agent + a CodedTool hitting the keyless Open-Meteo API. |
| `agent_network_designer` | Designs *other* agent networks into `registries/generated/`. |
| `agent_network_test_generator` | Generates test fixtures for an existing network. |

## finance_agent

A personal finance tracker built as an [AAOSA](registries/aaosa.hocon) network: a front man
that talks to the user, and one specialist agent per concern. The specialists never address
the user directly — the front man compiles their answers into a single reply.

```
finance_coach  (front man — routing, tone, final answer)
├── income_agent    salary, freelance, rent, bonuses          → record_income
├── spend_agent     logging spends, category analysis         → record_expense, spend_breakdown, list_transactions
├── balance_agent   month totals, savings rate, trend         → compute_balance
├── goals_agent     create/fund/track goals, required pace    → manage_goal, list_goals
└── insights_agent  the joined-up read, affordability, advice → financial_snapshot
```

Every agent also gets `current_period`, so relative dates ("yesterday", "last month") are
resolved by a tool rather than guessed by the model.

### Things worth knowing about the data model

- **Recurring entries are stored once and projected forward.** A salary or a rent payment is
  recorded a single time and counts in every subsequent month. The user never re-enters
  their salary, and re-stating it with a new figure is treated as a raise (an update), not a
  second income stream.
- **Amounts are stored as integer minor units** (paise/cents). Summing a year of floats
  drifts; integers do not. Input parsing accepts `95000`, `95k`, `1.2 lakh`, `Rs 28,000`.
- **Goal contributions are not expenses.** `balance` is income minus expenses;
  `allocated_to_goals` is reported alongside it, and `unallocated` is what remains after
  those transfers. Saving is not spending, so it is never double-counted.
- **Affordability is checked across all goals at once.** Several individually reasonable
  goals can be collectively impossible — `plan_is_affordable` and `monthly_shortfall` catch
  that, and `at_risk` flags a goal whose deadline demands more than the surplus allows.
- **Contributions already made this month are added back into the surplus** when judging
  affordability, otherwise funding a goal would make that same goal look unaffordable.

### Storage

One SQLite file, `finance_local.db` in the repo root, created on first use. Override with
`FINANCE_DB_PATH`. Currency is display-only and set by `FINANCE_CURRENCY` (default `INR`).

Every row is scoped to a user id read from `sly_data["user_id"]`, which stays out of the
chat stream. A local run gets `default`; a hosted deployment sets it per conversation and
the ledgers stay separate.

### Try it

```
My monthly salary is 95000. Set that up.
My rent is 28000 a month and I pay a 15000 EMI.
I spent 1200 on groceries and 800 on fuel yesterday.
What's my balance this month?
Add a goal: 3 lakh emergency fund by March 2027.
Put 20000 toward the emergency fund.
Where is my money going, and can I actually afford all my goals?
```
