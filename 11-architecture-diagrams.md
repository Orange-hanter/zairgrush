---
title: "ZAIrgRush — architecture in diagrams: agents, data flow, gates"
type: architecture
status: draft
version: 0.2
created: 2026-08-23
updated: 2026-08-23
related:
  - 05-agent-swarm.md
  - 06-knowledge-infra-experiments.md
  - 08-operators-guide.md
summary: >
  Mermaid views of the swarm as it is implemented, not as it was first
  sketched: who the agents are, what state they touch, the ordered chain of
  mechanical gates a round passes through, how a round is charged to the
  fix budget, how a verdict is validated or salvaged, and where the human
  is asked. Every node names a section of 05-agent-swarm.md and, where it
  exists, the module in tools/swarm/swarm that implements it.
---

# Architecture in diagrams

This document is a **view**, not a source of truth. The contracts live in
`05-agent-swarm.md`, the behaviour lives in `tools/swarm/swarm/`. Where a
diagram and the code disagree, the code is right and the diagram is a bug.

Diagrams are drawn against the code as of 2026-08-23 (design doc v0.50).
Section references are to `05-agent-swarm.md`.

## How to read these diagrams

Shape and colour mean the same thing in every figure below. Nothing else is
decorative: a box is a thing that acts, a **diamond is a check the
orchestrator performs itself** — no model, no money, just code looking at
files — and a cylinder is state that survives the process.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":26,"rankSpacing":70,"padding":12}}}%%
flowchart TB
    a["<b>MODEL CALL</b><br/>an agent runs · money is spent"]:::agent
    b{"<b>CHECK</b><br/>no model"}:::check
    c["<b>LOOP CODE</b><br/>the orchestrator itself"]:::mech
    d["<b>HUMAN</b><br/>the scarcest resource"]:::person
    e[("<b>STATE</b><br/>survives the process")]:::store
    f["<b>CLOSED</b><br/>task done, committed"]:::good
    g["<b>STOPPED</b><br/>a human is needed"]:::bad

    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2.5px,color:#1A1726
    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef store fill:#F4EFE4,stroke:#9C8455,stroke-width:1.5px,color:#33291A
    classDef person fill:#F3E8FF,stroke:#7C3AED,stroke-width:2px,color:#2A1149
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
```

Two conventions keep the figures readable. Edge labels are **never
sentences** — the words on a line name the thing being handed over
(`plan-diff`, `verdict`, `commit`), and the reasoning lives in the prose
beside the figure. A sentence parked on a long edge drifts to the middle of
the drawing and stops belonging to anything. And a chain is drawn **left to
right** while it still fits the page that way; the one chain that does not,
the round itself (§4), runs top to bottom, because a page scrolls downward
anyway.

---

## 1. Roles and the state they touch

Three expensive roles on strong models, one authoring role, one cheap third
circuit, and a single dumb process holding them together. The orchestrator
contains no LLM: all judgement is in the agents, all mechanics are in the
orchestrator (§3).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":38,"rankSpacing":95,"padding":12}}}%%
flowchart LR
    owner["<b>OWNER</b><br/>approves the plan once<br/>answers the inbox"]:::person
    plan["<b>PLANNER</b> · claude -p<br/>outside the loop, §3.1<br/>emits a plan-diff, never a queue"]:::agent

    orch["<b>ORCHESTRATOR</b><br/>swarm · python · no LLM<br/>the only long-lived process<br/><br/>loop · gitops · verdicts<br/>promptbuilder · driver"]:::mech

    subgraph AG[" the loop's two hands "]
        exec["<b>EXECUTOR</b> §3.2.0<br/>kimi | claude | ollama<br/>writes code · git is read-only"]:::agent
        rev["<b>REVIEWER</b> §4.2<br/>claude -p --json-schema<br/>read-only · judges the diff"]:::agent
    end

    help["<b>HELPERS</b> §7<br/>cheap models<br/>commit msg · dedup<br/>digests · embeddings<br/>fail-open, never blocking"]:::cheap
    doc["<b>DOCUMENTER</b> §3.3<br/>event-driven prose"]:::agent

    files[("<b>.swarm/</b><br/>tasks · state · journal<br/>metrics · board")]:::store
    pg[("<b>swarm_memory</b> E9<br/>lessons · FTS + vectors")]:::store
    git[("<b>git</b><br/>truth about code<br/>1 accepted round = 1 commit")]:::store

    owner -->|"goal"| plan
    plan -->|"plan-diff"| orch
    orch -->|"handoff&nbsp;&nbsp;⇄&nbsp;&nbsp;report"| exec
    orch -->|"diff&nbsp;&nbsp;⇄&nbsp;&nbsp;verdict"| rev
    orch -->|"optional"| help
    orch -->|"events"| doc
    orch -->|"sole writer"| files
    orch -->|"lessons"| pg
    exec -->|"writes"| git
    rev -.->|"reads"| git

    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2.5px,color:#1A1726
    classDef cheap fill:#F1F5F9,stroke:#94A3B8,stroke-width:1.5px,color:#1F2937
    classDef store fill:#F4EFE4,stroke:#9C8455,stroke-width:1.5px,color:#33291A
    classDef person fill:#F3E8FF,stroke:#7C3AED,stroke-width:2px,color:#2A1149
```

Two asymmetries carry most of the design's weight:

- **The reviewer never sees the executor's reasoning** — only requirements,
  the diff and the gate output. Otherwise it catches the same false
  assumptions and the second model stops being a second opinion (§3).
- **`.swarm/` is deliberately outside git** (`.git/info/exclude`, §6.3): git
  stores the *result*, not the run's state — using it as a state bus breaks
  atomic access to the queue.

The commit arrow is missing from the figure on purpose: the orchestrator
writes to git only through the commit step of a closed round, which is the
last node of figure 4.

---

## 2. Task status, and the reason it stopped

Statuses are the queue's contract (§4.3). The happy path is three
transitions wide; everything interesting is in *why* a task leaves it.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0"}}}%%
stateDiagram-v2
    direction LR
    [*] --> pending
    pending --> in_progress: picked
    in_progress --> in_review: gates passed
    in_review --> in_progress: request_changes
    in_review --> done: approve confirmed
    done --> [*]

    in_progress --> pending: not the task's fault
    in_progress --> blocked: no verdict
    in_review --> blocked: intent · rounds out
    blocked --> pending: human unblocks it
```

`pending` is not a failure state. A task stopped by budget, quota or a dead
executor is **not at fault**: its work goes to a stash and the queue resumes
with the same `go` (§5.3). Only `blocked` means the loop wants a human — and
it always names which of these it is, because "blocked" alone leaves the
operator in the dark (§4.2, §5.7).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":18,"rankSpacing":110,"padding":12}}}%%
flowchart LR
    b1["<b>baseline_red</b><br/>repo was already broken"]:::bad
    b2["<b>dispute</b><br/>executor cannot finish honestly"]:::bad
    b3["<b>ask_user</b><br/>a finding about intent"]:::bad
    b4["<b>invalid_verdict</b><br/>review never happened"]:::bad
    b5["<b>max_iterations</b><br/>fix rounds spent"]:::bad
    b6["<b>futile_rounds</b><br/>rounds burned before judgement"]:::bad
    b7["<b>integrity</b><br/>history or loop state moved"]:::bad
    b8["<b>crash</b><br/>run died, work rescued"]:::bad

    a1["fix the repository"]:::act
    a2["<b>swarm replan</b><br/>the plan was wrong"]:::act
    a3["<b>swarm answer</b><br/>decide the intent"]:::act
    a4["<b>swarm retry</b><br/>evidence kept, rounds restart"]:::act

    b1 --> a1
    b2 --> a2
    b3 --> a3
    b4 --> a4
    b5 --> a2
    b6 --> a4
    b7 --> a1
    b8 --> a4

    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef act fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
```

---

## 3. The frame around one task

Before any round runs, two checks decide whether the loop is allowed to
start at all — and they are the cheapest checks in the system, because
neither spends a token.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":34,"rankSpacing":80,"padding":12}}}%%
flowchart LR
    pick["<b>NEXT TASK</b><br/>status pending · deps done"]:::mech
    pre["<b>PREFLIGHT</b> §5.1<br/>worktree clean?<br/>snapshot dirt, HEAD, state"]:::check
    base{"<b>BASELINE GATE</b> §5.0<br/>tests green on HEAD<br/>before the agent runs?"}:::check
    red["<b>baseline_red</b><br/>blocked · zero iterations<br/>executor never called"]:::bad
    round["<b>THE ROUND</b><br/>figure 4<br/>repeats until an outcome"]:::mech
    out{"<b>OUTCOME</b>"}:::check
    ok["<b>done</b><br/>committed"]:::good
    again["<b>pending</b><br/>budget · quota · engine down"]:::store
    stop["<b>blocked</b><br/>with a named reason"]:::bad

    pick --> pre
    pre --> base
    base -->|"red"| red
    base -->|"green"| round
    round --> out
    out -->|"approve ×2"| ok
    out -->|"not at fault"| again
    out -->|"human needed"| stop

    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2.5px,color:#1A1726
    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef store fill:#F4EFE4,stroke:#9C8455,stroke-width:1.5px,color:#33291A
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
```

A red baseline blocks the task at **zero cost**: the executor is never
called, so the run does not pay for a repository that was already broken.
Without this rule one unfinished task poisons the whole queue — in BENCH-2 a
single red test sent four tasks into dispute and burned eight executor calls
for nothing (§5.0).

---

## 4. Inside one round — the gate chain

This is the spine of the system: an ordered chain of **mechanical** checks
around two model calls. Read the numbered spine downward; every branch that
leaves it to the side ends the round early (`loop.run_task`).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":30,"rankSpacing":58,"padding":10}}}%%
flowchart TB
    money{"<b>1 · BUDGET</b><br/>before every round<br/>not once per task"}:::check
    cap{"<b>2 · FUTILE CAP</b><br/>max_futile_rounds"}:::check
    impl["<b>3 · EXECUTOR</b><br/>skipped on a confirming round<br/>goal · task · acceptance<br/>repo map · feedback · lessons"]:::agent
    gate{"<b>4 · GATE</b> §6.6<br/>tests · lint · types<br/>whole suite, run by the loop"}:::check
    integ{"<b>5 · INTEGRITY</b> §6.1.1<br/>history and loop state<br/>untouched?"}:::check
    scope{"<b>6 · SCOPE</b> §6.2<br/>diff inside task paths?<br/>protected tests intact?"}:::check
    sig{"<b>7 · SIGNATURES</b><br/>skeleton contract intact?<br/>fill tasks only"}:::check
    rev["<b>8 · REVIEWER</b><br/>acceptance + diff + gate output<br/>returns a verdict"]:::agent
    pol["<b>9 · POLICIES</b><br/>run-level suppressions<br/>blocker is never suppressed"]:::mech
    tri["<b>10 · TRIAGE</b><br/>intent → the human<br/>mechanical → the executor"]:::mech
    dec["<b>11 · DECIDE</b><br/>figure 8"]:::mech

    stopb["<b>pending</b><br/>stash, resume later"]:::bad
    esc["<b>blocked</b><br/>futile_rounds"]:::bad
    nofrep["<b>FUTILE</b><br/>no report · crash<br/>2 instant deaths = engine down"]:::bad
    disp["<b>blocked</b><br/>dispute"]:::bad
    red["<b>PRODUCTIVE</b><br/>tests back to the executor<br/>reviewer not called"]:::warn
    ib["<b>blocked</b><br/>integrity"]:::bad
    sv["<b>FUTILE</b> + revert<br/>work destroyed<br/>nothing left to judge"]:::bad
    sf["<b>FUTILE</b>, no revert<br/>the body may be salvageable"]:::bad
    rf["<b>blocked</b><br/>invalid_verdict<br/>figure 7"]:::bad

    money -->|"ok"| cap
    cap -->|"ok"| impl
    impl -->|"report"| gate
    gate -->|"green"| integ
    integ -->|"ok"| scope
    scope -->|"ok"| sig
    sig -->|"ok"| rev
    rev -->|"verdict"| pol
    pol --> tri
    tri --> dec

    money -->|"spent"| stopb
    cap -->|"burned"| esc
    impl -->|"no report"| nofrep
    impl -->|"dispute"| disp
    gate -->|"red"| red
    integ -->|"violated"| ib
    scope -->|"violated"| sv
    sig -->|"drifted"| sf
    rev -->|"no verdict"| rf

    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2px,color:#1A1726
    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef warn fill:#FFF1E0,stroke:#C2661F,stroke-width:1.5px,color:#4A2408
```

Three properties worth stating in words, because the drawing hides them:

- **The gate runs the whole suite.** Narrowing it to the current task's tests
  is the only way to miss a regression in a neighbouring module (§5.0).
- **A gate failure is cheap.** The reviewer is not called at all; the executor
  gets the test output straight back and the round costs one call, not two.
- **A confirming round skips step 3 entirely.** It is a *second reading of the
  same diff*, not another lap of work, so the executor cannot quietly change
  the code the second opinion is about.

---

## 5. What a round is charged to

The most counter-intuitive rule in the loop, and the one measured hardest:
**only a round that reached a mechanical judgement spends the fix budget**
(§5.3). A round that collapsed earlier judged nothing, and charging it made
the loop announce "rounds exhausted" about work no one had ever looked at.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":34,"rankSpacing":85,"padding":12}}}%%
flowchart LR
    j1["gate ruled<br/>green or red"]:::src
    j2["reviewer returned<br/>a verdict"]:::src
    f1["executor died<br/>or sent no report"]:::src
    f2["scope guard<br/>reverted the work"]:::src
    f3["frozen signatures<br/>drifted"]:::src

    prod["<b>PRODUCTIVE</b><br/>the work was judged<br/>spends max_iterations"]:::good
    fut["<b>FUTILE</b><br/>nothing was judged<br/>spends max_futile_rounds"]:::bad

    e1["<b>escalate</b><br/>diagnosis from evidence<br/>figure 6"]:::warn
    e2["<b>blocked: futile_rounds</b><br/>names what burned them:<br/>quota · timeouts · reverts"]:::warn

    j1 --> prod
    j2 --> prod
    f1 --> fut
    f2 --> fut
    f3 --> fut
    prod -->|"limit hit"| e1
    fut -->|"cap hit"| e2

    classDef src fill:#FFFFFF,stroke:#9A93B0,stroke-width:1.2px,color:#1A1726
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef warn fill:#FFF1E0,stroke:#C2661F,stroke-width:1.5px,color:#4A2408
```

---

## 6. The diagnosis attached to an escalation

An escalation carries a hypothesis about the cause — but a hypothesis nobody
checks decays into a slogan. `_diagnose` walks a **closed list of mechanical
evidence**, most precise first, and stops at the first one that fires
(§5.7.0).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":22,"rankSpacing":52,"padding":10}}}%%
flowchart LR
    s1{"signature<br/>violations ≥ 2"}:::check
    s2{"scope<br/>violations ≥ 2"}:::check
    s3{"executor<br/>failures ≥ 2"}:::check
    s4{"two reviewers<br/>split on one diff"}:::check
    s5{"wide front<br/>that never shrinks"}:::check
    s6["<b>no mechanical cause</b><br/>report the trajectory<br/>of the last four rounds"]:::mech

    o1["the skeleton contract<br/>keeps drifting"]:::out
    o2["the boundary is wrong,<br/>not the work"]:::out
    o3["the executor cannot<br/>run in this environment"]:::out
    o4["reviewer spread,<br/>not task trouble"]:::out
    o5["size — the only case<br/>where splitting is advised"]:::out

    s1 -->|"no"| s2
    s2 -->|"no"| s3
    s3 -->|"no"| s4
    s4 -->|"no"| s5
    s5 -->|"no"| s6
    s1 -->|"yes"| o1
    s2 -->|"yes"| o2
    s3 -->|"yes"| o3
    s4 -->|"yes"| o4
    s5 -->|"yes"| o5

    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef out fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2px,color:#1A1726
```

The old fallback — "rounds exhausted, the task is probably too large" — was
wrong six times out of six on the frozen gold set, and each time it sent the
owner to split a task whose real trouble was elsewhere. Size is now named
only when the numbers point at it: at least three categories and at least
three findings per round, not shrinking.

---

## 7. One round as a conversation

The same round as figure 4, but showing **who holds what** at each moment.
Note where the two model calls sit and how little each of them is told.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"14px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","actorBkg":"#EFEAF8","actorBorder":"#7A5CB8","noteBkgColor":"#FFF4D6","noteBorderColor":"#D19A00"}}}%%
sequenceDiagram
    autonumber
    participant H as Owner
    participant O as Orchestrator
    participant E as Executor
    participant G as Gate
    participant R as Reviewer
    participant M as Memory
    participant Git as git

    O->>Git: status, rev-parse HEAD
    O->>G: baseline run on HEAD
    G-->>O: green — red blocks at zero cost

    O->>M: retrieve lessons for this task
    M-->>O: up to 5 lessons, ~2000 chars
    O->>E: goal + task + acceptance + repo map + findings + lessons
    Note over E: writes only inside allowed paths —<br/>git is read-only, the orchestrator commits
    E-->>O: report: status, summary, evidence, dispute?

    O->>G: full suite — run by the loop, not the executor
    G-->>O: tail of output
    O->>Git: diff --name-only for scope and protected tests
    O->>Git: work diff, long files condensed with sha256

    O->>R: rules + acceptance + decisions + norms + DIFF + gate output
    Note over R: read-only tools, no Bash —<br/>may return verification_requests
    R-->>O: verdict: analysis, verdict, summary, findings

    opt verification enabled for this task
        O->>G: whitelisted kinds only — unittest, git_show, python
        G-->>O: sanitized output
        O->>R: second pass with the results
        R-->>O: refined verdict — failure keeps the first
    end

    O->>O: policies, triage, decide
    alt approve confirmed twice on the same diff
        O->>Git: commit, message drafted by a cheap helper
        O->>M: record the lesson from the outcome
    else findings about intent
        O->>H: inbox question qNNN plus stash
    else mechanical findings
        O->>E: next round with findings only
    end
```

---

## 8. The verdict contract — form, meaning, rescue

A verdict is the only thing that can close a task, so its failure modes get
their own machinery (§4.2, §4.2.1). Measured on E13: **5 reviewer calls out of
17 returned no valid verdict and burned 37 % of all review money.**

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":30,"rankSpacing":70,"padding":12}}}%%
flowchart LR
    rc["<b>REVIEWER CALL</b><br/>claude -p --json-schema"]:::agent
    env{"envelope carries<br/>structured output?"}:::check
    salv["<b>SALVAGE</b> §4.2.1<br/>take the last tool call<br/>in the stream, reassemble"]:::mech
    val{"<b>VALIDATE</b><br/>form AND meaning"}:::check
    why["<b>WHY IT FAILED</b><br/>verdict outside the enum ·<br/>severity or category unknown ·<br/>approve over a blocker ·<br/>changes with zero findings ·<br/>analysis or summary is a stub"]:::warn
    retry{"same prompt would<br/>end the same way?"}:::check
    again["<b>RE-ASK ONCE</b><br/>reason appended at the tail,<br/>never in the cached prefix"]:::mech
    okv["<b>ACCEPTED</b><br/>salvaged ones marked<br/>in journal and metrics"]:::good
    fail["<b>invalid_verdict</b><br/>blocked · stash · inbox question<br/>diagnosis · evidence kept"]:::bad

    rc --> env
    env -->|"yes"| val
    env -->|"empty"| salv
    salv -->|"recovered"| val
    salv -->|"nothing"| fail
    val -->|"passes"| okv
    val -->|"fails"| why
    why --> retry
    retry -->|"yes"| fail
    retry -->|"no"| again
    again --> rc

    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2px,color:#1A1726
    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef warn fill:#FFF1E0,stroke:#C2661F,stroke-width:1.5px,color:#4A2408
```

Two rules in that figure are easy to miss. **A deterministic refusal is not
retried** — a call cut off by `budget_exhausted` ends at the same place on
the second attempt, and on PILOT-1 that cost $3.23 on top of $3.37 for two
truncated calls and zero verdicts. And **the retry reason goes in the tail of
the prompt**, behind the diff: in the stable prefix it would void the cache
for the entire task.

A salvaged verdict gets no leniency: an unparsed findings list is *not* an
empty one — substituting `[]` would silently turn `request_changes` into
`approve`.

---

## 9. How an accepted verdict becomes an outcome

`verdicts.decide` applies five tests in a fixed order. The order is the
design: an approve is counted before anything else, and non-convergence is
detected before the loop is allowed to spend another round on it.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":26,"rankSpacing":62,"padding":10}}}%%
flowchart LR
    a{"approve?"}:::check
    cnt{"seconded on<br/>this same diff?"}:::check
    b{"blocked?"}:::check
    r{"fix rounds<br/>spent?"}:::check
    nc{"same categories,<br/>count not shrinking?"}:::check
    it{"any finding<br/>about intent?"}:::check

    done["<b>DONE</b> · exit 0<br/>commit, task closed"]:::good
    cf["<b>CONFIRM</b> · exit 10<br/>re-review the same diff<br/>executor not called"]:::good
    em["<b>ESCALATE</b><br/>exit 20 · rounds exhausted"]:::bad
    en["<b>NONCONVERGENT</b><br/>exit 20 · needs a diagnosis,<br/>not another lap"]:::bad
    au["<b>ASK USER</b> · exit 11<br/>the owner decides intent"]:::warn
    cont["<b>CONTINUE</b> · exit 10<br/>mechanical findings<br/>back to the executor"]:::mech

    a -->|"yes"| cnt
    cnt -->|"yes"| done
    cnt -->|"no"| cf
    a -->|"no"| b
    b -->|"yes"| em
    b -->|"no"| r
    r -->|"yes"| em
    r -->|"no"| nc
    nc -->|"yes"| en
    nc -->|"no"| it
    it -->|"yes"| au
    it -->|"no"| cont

    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef warn fill:#FFF1E0,stroke:#C2661F,stroke-width:1.5px,color:#4A2408
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2px,color:#1A1726
```

Approvals are counted **per diff hash**, not per task: an approve earned
before a later fix belongs to a different diff and does not count. Without
that rule a chain of approve → flip → fix → approve closed tasks whose final
code exactly one reviewer had ever seen.

Between rounds the loop also keeps the **best** round — the one with fewest
findings — and rolls back to it if the next is worse. Never on a confirming
round, though: there the code did not change, so a different finding count is
reviewer spread, not regression.

---

## 10. Context flow — who is told what

Context is a budget, not a courtesy (§8). Two rules shape every prompt: the
**asymmetry** that keeps the reviewer independent, and the **cache prefix**
that decides what a call costs.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":22,"rankSpacing":95,"padding":12}}}%%
flowchart LR
    goal["goal · task · acceptance"]:::src
    map["repo map — ranked signatures<br/>~25 lines, skipped on local edits"]:::src
    fb["last verdict's<br/>mechanical findings"]:::src
    les["lessons from memory"]:::src
    hum["the owner's decision"]:::src
    diff["git diff — files over ~400 lines<br/>condensed to sha256 + excerpt,<br/>with the hiding declared"]:::src
    tests["gate output"]:::src

    ex["<b>EXECUTOR PROMPT</b><br/>goal · lessons · task · acceptance<br/>repo map · feedback · constraints<br/>dispute channel · output schema"]:::agent

    subgraph RV["REVIEWER PROMPT — cut on cache boundaries"]
        r1["<b>① RULES</b><br/>stable across all tasks<br/>review rules · injection rule · lens"]:::agent
        r2["<b>② TASK</b><br/>stable within the task<br/>acceptance · decisions · norms"]:::agent
        r3["<b>③ TAIL</b><br/>volatile — diff, then gate output,<br/>then verification and retry notes"]:::agent
    end

    goal --> ex
    map --> ex
    fb --> ex
    les --> ex
    hum --> ex
    goal --> r2
    hum --> r2
    les --> r2
    diff --> r3
    tests --> r3
    ex -.->|"never forwarded"| RV

    classDef src fill:#FFFFFF,stroke:#9A93B0,stroke-width:1.2px,color:#1A1726
    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
```

- **The executor never receives** previous handoffs or its own past
  reasoning; **the reviewer never receives** the executor's report. The dotted
  edge is the one that must stay severed (§3, §8).
- **Block order is money.** Prompt caching matches on a *prefix*: the first
  differing byte voids everything after it, a write costs twice the input
  rate, a read a tenth of it. Hence stable → task-stable → volatile, with the
  diff placed *before* the gate output because it is the largest block and
  must not sit behind anything that changes between rounds.
- **Reviewer hands are drawn by lottery** per call from
  `review_model_pool` / `review_effort_pool`, recorded in metrics
  (`swarm ab`). Because every task is reviewed twice on the same diff, the
  per-call draw produces "two hands on one diff" comparisons for free (§8.2).

---

## 11. Where the human is asked

The loop is an autopilot with a **named** list of escalations (§1.1). The
scarcest resource in the system is the owner's attention, so the triage rule
is explicit: of 6–7 findings per task, 1–2 reach the human (§5.7).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":24,"rankSpacing":80,"padding":12}}}%%
flowchart LR
    outc["<b>A TERMINAL OUTCOME</b><br/>ask_user · dispute · invalid_verdict<br/>escalate · futile · baseline · crash"]:::bad

    ask["<b>state.ask</b><br/>question qNNN carries<br/>diagnosis · findings · round · stash<br/><br/>work is stashed, never discarded"]:::mech

    subgraph SF["surfaces — one event, one name · vocab.py"]
        b1["<b>swarm board</b><br/>the run, one page"]:::surf
        b2["<b>swarm inbox</b><br/>the open questions"]:::surf
        b3["<b>swarm why</b><br/>why this task stopped"]:::surf
        b4["<b>swarm report</b><br/>the run as prose"]:::surf
    end

    owner["<b>OWNER</b><br/>decides"]:::person

    a1["<b>answer</b> --add-path<br/>binds the WHOLE task,<br/>shown to the reviewer<br/>as not open to dispute"]:::act
    a2["<b>policy add</b><br/>suppression by the orchestrator,<br/>the reviewer stays sighted"]:::act
    a3["<b>replan</b><br/>split · widen paths ·<br/>carve out a new task"]:::act
    a4["<b>retry</b><br/>rounds restart,<br/>evidence keeps its own name"]:::act

    queue["<b>swarm go</b><br/>the queue continues"]:::good

    outc --> ask
    ask --> SF
    SF --> owner
    owner --> a1
    owner --> a2
    owner --> a3
    owner --> a4
    a1 --> queue
    a2 --> queue
    a3 --> queue
    a4 --> queue

    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2px,color:#1A1726
    classDef surf fill:#F1F5F9,stroke:#94A3B8,stroke-width:1.5px,color:#1F2937
    classDef person fill:#F3E8FF,stroke:#7C3AED,stroke-width:2px,color:#2A1149
    classDef act fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
```

The rule the pilot paid for: **an escalation must carry a diagnosis.** A task
that goes to `blocked` with no journal entry and no question leaves the
operator blind — the work may have been finished and green, with only the
review having failed (§4.2).

---

## 12. Memory between runs (E9)

Memory is an **observation**, not part of the work: its failure leaves a trace
and never costs a run. Every path degrades — Postgres to a local file scan,
retrieval to nothing at all.

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":30,"rankSpacing":80,"padding":12}}}%%
flowchart LR
    task["<b>TASK ENDS</b><br/>any terminal outcome"]:::mech
    rec["<b>record_task_outcome</b><br/>one lesson, 700 chars max<br/>useful · dead_end · corrected"]:::mech
    files[("<b>.swarm/memory/*.md</b><br/>the files are the truth")]:::store
    pgi[("<b>swarm_memory</b><br/>FTS index + vectors")]:::store
    runend["<b>RUN ENDS</b>"]:::mech
    refl["<b>reflect_after_run</b><br/>cheap helper rebuilds<br/>the digest"]:::cheap
    less[("<b>LESSONS.md</b><br/>the digest a human reads")]:::store
    ret["<b>retrieve</b><br/>FTS, then local scan<br/>top-5 · ~2000 chars<br/>never raises"]:::mech
    inj["<b>INJECTED</b><br/>lessons into the handoff,<br/>norms into the review prompt"]:::agent

    task --> rec
    rec --> files
    files --> pgi
    runend --> refl
    refl --> less
    refl --> pgi
    pgi -.->|"first"| ret
    files -.->|"fallback"| ret
    ret --> inj

    classDef mech fill:#FFFFFF,stroke:#4A2E78,stroke-width:2px,color:#1A1726
    classDef store fill:#F4EFE4,stroke:#9C8455,stroke-width:1.5px,color:#33291A
    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef cheap fill:#F1F5F9,stroke:#94A3B8,stroke-width:1.5px,color:#1F2937
```

Measured effect (E9, planner arm): with memory, a replan folded all three of
the owner's decisions into named tasks, including a satellite inherited from
a different dispute; without memory that satellite was lost in every replan.

---

## 13. Failure ladder: quota, crashes, liveness

Every branch here exists because the loop once mistook one kind of failure
for another and charged the wrong party (§5.2, §5.3).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","edgeLabelBackground":"#FFFFFF"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":24,"rankSpacing":78,"padding":12}}}%%
flowchart LR
    acall["<b>AN AGENT CALL</b><br/>executor or reviewer"]:::agent

    silence{"silence<br/>over 600 s"}:::check
    wall["<b>wall clock</b><br/>over 30 min"]:::check
    words["<b>quota wording</b><br/>session · rate · usage<br/>limit · 429"]:::check
    z403["<b>403, zero work</b><br/>no tokens, no cost"]:::check
    p403["<b>403, tokens spent</b><br/>the provider did work"]:::check
    dead["<b>instant death ×2</b><br/>seconds, empty stream"]:::check

    alive["<b>KEEP WAITING</b><br/>silence is not death"]:::good
    restart["<b>RESTART</b><br/>kill · stash · fresh agent<br/>counts as a round · 2 = blocked"]:::warn
    quota["<b>QUOTA PAUSE</b><br/>not an agent error<br/>backoff 1 → 2 → 4 min"]:::warn
    notq["<b>INVESTIGATE</b><br/>this refusal is not quota<br/>and must not be waited out"]:::bad
    unav["<b>ENGINE DOWN</b><br/>task to pending, run stops"]:::bad

    auto{"quota_resume<br/>= auto?"}:::check
    exit4["<b>EXIT 4</b><br/>stash · task pending ·<br/>the human decides when"]:::bad
    guard{"within 3 resumes<br/>and 6 hours?"}:::check
    cont["<b>RESUME</b><br/>sleep past the reset +90 s<br/>every resume is journalled"]:::good

    acall --> silence
    acall --> wall
    acall --> words
    acall --> z403
    acall --> p403
    acall --> dead

    silence -->|"stream moving"| alive
    silence -->|"stalled"| restart
    wall --> restart
    words --> quota
    z403 --> quota
    p403 --> notq
    dead --> unav
    quota --> auto
    auto -->|"no · default"| exit4
    auto -->|"yes"| guard
    guard -->|"no"| exit4
    guard -->|"yes"| cont

    classDef agent fill:#E8F0FE,stroke:#3B6FD4,stroke-width:1.5px,color:#12233F
    classDef check fill:#FFF4D6,stroke:#D19A00,stroke-width:1.5px,color:#3A2C05
    classDef good fill:#E4F6EA,stroke:#2F8F5F,stroke-width:1.5px,color:#0F3D24
    classDef bad fill:#FDECEA,stroke:#C2453B,stroke-width:1.5px,color:#4A130E
    classDef warn fill:#FFF1E0,stroke:#C2661F,stroke-width:1.5px,color:#4A2408
```

The same defect class — "a refusal by quota looks like a bad answer" — has
been fixed three times at three different levels. Read the branches above as
scar tissue, not as configuration. The distinction that took longest to
learn is the last one on the left: a 403 that spent **no** tokens is a quota
wall and joins the backoff ladder, while a 403 that **did** spend tokens is a
real refusal and must be investigated, not slept off.

---

## 14. CLI surface

One entry point, `swarm`; the commands map onto the subsystems above (§9.2).

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontFamily":"ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif","fontSize":"15px","primaryColor":"#EFEAF8","primaryTextColor":"#1A1726","primaryBorderColor":"#7A5CB8","lineColor":"#9A93B0","clusterBkg":"#F8F6FC","clusterBorder":"#D8D0EA"},"flowchart":{"diagramPadding":26,"curve":"linear","nodeSpacing":16,"rankSpacing":70,"padding":12}}}%%
flowchart LR
    subgraph RUN["run it"]
        c1["<b>go</b><br/>plan and execute, end to end"]:::cmd
        c2["<b>run · resume</b><br/>execute the existing queue"]:::cmd
        c3["<b>plan · replan</b><br/>the planner alone"]:::cmd
        c4["<b>retry</b><br/>return a blocked task"]:::cmd
    end
    subgraph SEE["see it"]
        c5["<b>board · status</b><br/>where the run is now"]:::cmd
        c6["<b>why</b><br/>why this task stopped"]:::cmd
        c7["<b>report · ab</b><br/>the run as prose, arms"]:::cmd
        c8["<b>map · impact</b><br/>symbols and their callers"]:::cmd
    end
    subgraph DEC["decide"]
        c9["<b>inbox · answer</b><br/>the human channel"]:::cmd
        c10["<b>policy</b><br/>suppress a class of finding"]:::cmd
    end
    subgraph MEM["remember"]
        c11["<b>memory</b><br/>add · search · show · forget"]:::cmd
        c12["<b>memory</b><br/>reflect · reindex · sync"]:::cmd
    end
    c13["<b>doctor</b><br/>is the environment fit to run"]:::cmd

    st[(".swarm/ and git")]:::store
    pgs[("swarm_memory")]:::store

    RUN --> st
    SEE --> st
    DEC --> st
    MEM --> pgs

    classDef cmd fill:#FFFFFF,stroke:#4A2E78,stroke-width:1.5px,color:#1A1726
    classDef store fill:#F4EFE4,stroke:#9C8455,stroke-width:1.5px,color:#33291A
```

---

## Change log

### v0.2 (2026-08-23)

Diagrams redrawn for legibility after the first edition proved hard to read
at any zoom. The diagnosis was one mechanism with two symptoms: a sentence
used as an edge label is placed at the **midpoint** of its edge, so on the
long edges of a hub-and-spoke layout the text drifted into open space and
stopped belonging to anything, while the figures it bloated grew too large
to render at a readable size.

Three rules now hold across every figure. An edge label is at most three
words and names the thing handed over — the reasoning moved into the prose
beside the figure. Every edge spans **one rank**, so a label always sits
between its two endpoints. And each figure declares the same shape and
colour vocabulary, introduced by a legend in the opening section.

Structural changes from the same pass: the monolithic gate-chain figure was
split into the task frame (§3) and the round itself (§4), and the round was
turned top-to-bottom, which cut its width from 3506 px to 1184 px and let it
fit a reading column; the task state machine was split into the status flow
and a reason → remedy map (§2); the five verdict rejection rules moved off
five separate edges into one node (§8); and the failure ladder's branch
conditions became nodes instead of sentence-labels (§13).

Sixteen figures, all verified twice: rendered with `mmdc`, then measured in a
browser, where every one of the 322 node labels fits inside the box drawn for
it. Two defects were found only by that second check — Mermaid under-measures
`<b>` text, so a bold segment on a label's longest line clips, and it caps a
label at 200 px, so a longer line overflows however wide the node looks.

### v0.1 (2026-08-23)

First edition. Eleven Mermaid views drawn from `05-agent-swarm.md` v0.50 and
from the implementation in `tools/swarm/swarm/` (`loop.run_task`,
`verdicts.decide`, `gitops`, `reviewer`, `promptbuilder`, `helpers`,
`meminject`). Written in English per the owner's standing rule for new
documents; the surrounding Russian documents are untouched.
