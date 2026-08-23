---
title: "ZAIrgRush — architecture in diagrams: agents, data flow, gates"
type: reference
status: draft
version: 0.1
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

---

## 1. Roles and the state they touch

Three expensive roles on strong models, one authoring role, one cheap third
circuit, and a single dumb process holding them together. The orchestrator
contains no LLM: all judgement is in the agents, all mechanics are in the
orchestrator (§3).

```mermaid
flowchart TB
    human["OWNER<br/>approves the plan once · answers the inbox<br/>the scarcest resource in the system"]
    plan["PLANNER — claude -p, outside the loop, §3.1<br/>emits a plan-diff, never a new tasks.json"]

    orch["ORCHESTRATOR — swarm<br/>Python, NO LLM, the only long-lived process<br/>―――<br/>loop.py — task cycle, stop conditions<br/>gitops.py — gate, scope, integrity, revert, commit<br/>verdicts.py — validation, policies, decide<br/>promptbuilder.py — handoff and review prompts<br/>driver.py — stream-json runner, heartbeat, envelopes"]

    subgraph expensive["Expensive roles — strong models, strict JSON contracts"]
        direction LR
        exec["EXECUTOR, §3.2.0<br/>engine = kimi | claude | ollama<br/>writes code, returns a JSON report<br/>git is READ-ONLY for it"]
        rev["REVIEWER, §4.2<br/>claude -p --json-schema<br/>read-only, returns a verdict<br/>never sees the executor's reasoning"]
    end

    doc["DOCUMENTER, §3.3 — strong model, event-driven<br/>input = structural data, output = md"]
    help["HELPERS, §7 — cheap models, fail-open<br/>commit message · findings dedup · stagnation hint<br/>log digest · embeddings<br/>never decides anything that touches code"]

    subgraph state["STATE — files, not a message bus, §3 and §6.3"]
        direction LR
        tasks[".swarm/tasks.json<br/>the queue"]
        st[".swarm/state.json<br/>statuses, inbox,<br/>policies, locks"]
        jrnl[".swarm/log/run.jsonl — facts<br/>.swarm/metrics.jsonl — money, time<br/>.swarm/log/NNN-*.json — raw streams"]
        board[".swarm/board.html<br/>one-page run board"]
    end

    git[("git<br/>the only source of truth about code<br/>one accepted round = one commit")]
    pg[("PostgreSQL swarm_memory, E9<br/>lessons: FTS + vectors<br/>degrades to a local file scan")]

    human -->|"swarm plan / go"| plan
    plan -->|"plan-diff, validated as a transaction"| orch
    human -->|"swarm answer qNNN"| orch
    orch -->|"escalation carrying a diagnosis"| human

    orch -->|"handoff: task + acceptance<br/>+ repo map + feedback"| exec
    exec -->|"report: done / no_change_needed / dispute"| orch
    orch -->|"acceptance<br/>+ diff + gate output"| rev
    rev -->|"verdict: approve / request_changes / blocked"| orch

    orch -->|"milestones, experiments, escalations"| doc
    orch <-->|"cheap, optional, never blocking"| help

    exec -->|"writes only inside task paths"| git
    rev -.->|"reads code read-only"| git
    orch -->|"commits after a confirmed approve"| git
    orch -->|"sole writer: atomic rename + flock"| state
    orch <-->|"record after each task · reflect after the run ·<br/>inject into prompts"| pg

    classDef agent fill:#eef6ff,stroke:#3b82f6,stroke-width:1px
    classDef mech fill:#f4f4f5,stroke:#71717a,stroke-width:2px
    classDef store fill:#fff7e6,stroke:#d19a00,stroke-width:1px
    classDef person fill:#f3e8ff,stroke:#7c3aed,stroke-width:1px
    class exec,rev,plan,doc,help agent
    class orch mech
    class tasks,st,jrnl,board,git,pg store
    class human person
```

Two asymmetries carry most of the design's weight:

- **The reviewer never sees the executor's reasoning** — only requirements,
  the diff and the gate output. Otherwise it catches the same false
  assumptions and the second model stops being a second opinion (§3).
- **`.swarm/` is deliberately outside git** (`.git/info/exclude`, §6.3): git
  stores the *result*, not the run's state — using it as a state bus breaks
  atomic access to the queue.

---

## 2. Task state machine

Statuses are the queue's contract (§4.3). The interesting part is not the
happy path but the named reasons a task lands in `blocked`: each one has its
own escalation text, because "blocked" alone leaves the operator in the dark
(§4.2, §5.7).

```mermaid
stateDiagram-v2
    [*] --> pending: planner writes the task, paths + acceptance mandatory

    pending --> in_progress: picked by ready_tasks, deps done, budget left
    in_progress --> blocked_baseline: baseline gate red, agent never called
    in_progress --> in_review: gate + scope + integrity passed
    in_review --> in_progress: request_changes, mechanical findings to executor
    in_review --> done: approve confirmed N times on the SAME diff

    in_progress --> pending: budget exhausted mid-task, quota pause, executor unavailable

    in_progress --> blocked_dispute: executor dispute, §4.4
    in_progress --> blocked_integrity: loop-owned files or history touched
    in_progress --> blocked_review: review never happened, invalid_verdict
    in_progress --> blocked_crash: run crashed, work rescued to stash
    in_review --> blocked_ask: findings about INTENT, only the owner knows
    in_review --> blocked_max: max_iterations or nonconvergent, with a diagnosis
    in_progress --> blocked_futile: futile_rounds, burned before any judgement

    state "blocked: baseline_red" as blocked_baseline
    state "blocked: dispute" as blocked_dispute
    state "blocked: integrity" as blocked_integrity
    state "blocked: invalid_verdict" as blocked_review
    state "blocked: crash" as blocked_crash
    state "blocked: ask_user" as blocked_ask
    state "blocked: max_iterations | nonconvergent" as blocked_max
    state "blocked: futile_rounds" as blocked_futile

    blocked_ask --> pending: swarm answer qNNN, decision binds the WHOLE task
    blocked_dispute --> pending: swarm replan, planner changes the plan
    blocked_max --> pending: swarm retry
    blocked_futile --> pending: swarm retry after fixing the environment
    blocked_baseline --> pending: human fixes the red repository
    blocked_integrity --> pending: human resolves the divergence
    blocked_review --> pending: swarm retry, evidence kept under its own name
    blocked_crash --> pending: swarm retry, the stash is for the human

    done --> [*]
```

`pending` is not a failure state: a task stopped by budget, quota or a dead
executor is **not at fault**, its work goes to a stash and the queue resumes
with the same `go` (§5.3).

---

## 3. The gate chain — one round, in the real order

This is the spine of the system: an ordered chain of **mechanical** checks
around two model calls. Order is not decorative — each check exists to keep
the next one from judging the wrong thing (`loop.run_task`).

```mermaid
flowchart TD
    start(["Task selected: deps done, status pending"]) --> pre

    pre["PREFLIGHT, §5.1<br/>worktree clean? pre-existing dirt snapshot,<br/>HEAD and state fingerprint recorded"]
    pre --> base

    base{"BASELINE GATE, §5.0<br/>tests green on current HEAD?"}
    base -->|"red"| baseblock["blocked: baseline_red<br/>ZERO iterations, executor never called,<br/>no tokens spent"]
    base -->|"green"| roundtop

    roundtop{"Budget left? §5.3<br/>checked before EVERY round,<br/>not once per task"}
    roundtop -->|"no"| bstop["stash · task back to pending<br/>journal: budget_exhausted"]
    roundtop -->|"yes"| futilecap

    futilecap{"Futile rounds under max_futile_rounds?"}
    futilecap -->|"no"| fesc["escalate: futile_rounds<br/>names what the rounds burned on"]
    futilecap -->|"yes"| conf

    conf{"Confirming round?"}
    conf -->|"yes — same diff, executor NOT called"| gate
    conf -->|"no"| impl

    impl["1 · IMPLEMENT<br/>executor engine per config<br/>handoff = goal + task + acceptance +<br/>repo map + feedback + lessons"]
    impl --> report{"Valid JSON report?"}
    report -->|"no report / crash"| futile1["FUTILE round<br/>two instant crashes = executor_unavailable"]
    report -->|"dispute"| disp["blocked: dispute<br/>stash + inbox question + planner replan"]
    report -->|"done or no_change_needed"| gate

    gate{"2 · GATE, §6.6<br/>orchestrator runs tests, lint, types itself<br/>whole suite, never narrowed to the task"}
    gate -->|"red"| gfail["PRODUCTIVE round<br/>output back to the executor,<br/>reviewer NOT called — cheap iteration"]
    gate -->|"green"| integ

    integ{"3 · INTEGRITY, §6.1.1<br/>history and loop-owned state untouched?"}
    integ -->|"violated"| iblock["blocked: integrity<br/>wording neutral: the check knows the FACT,<br/>not the author"]
    integ -->|"ok"| scope

    scope{"4 · SCOPE, §6.2 and §5.5<br/>diff inside task paths?<br/>protected tests untouched?"}
    scope -->|"violated"| srev["revert + FUTILE round<br/>work destroyed — nothing to judge"]
    scope -->|"ok"| sig

    sig{"5 · FROZEN SIGNATURES, E10 fill tasks<br/>skeleton contract intact?"}
    sig -->|"changed"| sfut["FUTILE round, NO revert<br/>boundaries are about OTHERS' code,<br/>signatures about your own"]
    sig -->|"ok"| review

    review["6 · REVIEW<br/>claude -p --json-schema<br/>acceptance + diff + gate output"]
    review --> vok{"Verdict valid in FORM and MEANING?"}
    vok -->|"no, after salvage and one retry"| rfail["blocked: invalid_verdict<br/>+ diagnosis in the inbox — the work<br/>may well have been green"]
    vok -->|"yes"| pol

    pol["7 · POLICIES — verdicts.apply_policies<br/>run-level suppressions applied HERE,<br/>never in the reviewer's prompt: filtering<br/>by instruction costs recall.<br/>blocker is never suppressed"]
    pol --> cls["8 · TRIAGE — classify_findings<br/>architecture and scope = INTENT, to the human<br/>everything else = mechanical, to the executor"]
    cls --> dec["9 · decide — see diagram 6"]

    gfail -.->|"next round"| roundtop
    futile1 -.-> roundtop
    srev -.-> roundtop
    sfut -.-> roundtop

    classDef bad fill:#fdeaea,stroke:#c0392b
    classDef check fill:#fef9e7,stroke:#b7950b
    class baseblock,bstop,fesc,disp,iblock,rfail,futile1,srev,sfut bad
    class base,roundtop,futilecap,gate,integ,scope,sig,vok,report,conf check
```

Three properties worth stating in words, because a diagram hides them:

- **The gate runs the whole suite.** Narrowing it to the current task's tests
  is the only way to miss a regression in a neighbouring module (§5.0).
- **A gate failure is cheap.** The reviewer is not called at all; the executor
  gets the test output straight back.
- **A confirming round does not call the executor.** It is a *second reading
  of the same diff*, not another lap of work (§5.7, `loop.run_task`).

---

## 4. What a round is charged to — productive vs futile

The most counter-intuitive rule in the loop, and the one measured hardest:
**only a round that reached a mechanical judgement spends the fix budget**
(§5.3). A round that collapsed earlier judged nothing, and charging it made
the loop announce "rounds exhausted" about work no one had ever looked at.

```mermaid
flowchart LR
    r["Round ends"] --> q{"Did anything JUDGE the work?"}

    q -->|"gate ruled, green or red"| p["PRODUCTIVE<br/>productive += 1<br/>counts against max_iterations"]
    q -->|"reviewer returned a verdict"| p

    q -->|"executor died or returned no valid report"| f["FUTILE<br/>futile += 1<br/>counts against max_futile_rounds"]
    q -->|"scope guard reverted the work"| f
    q -->|"frozen signatures drifted"| f

    p --> pcap{"productive >= max_iterations + confirm_rounds?"}
    pcap -->|"yes"| pesc["escalate_max — diagnosis from evidence"]
    pcap -->|"no"| next["next round"]

    f --> fcap{"futile >= max_futile_rounds?"}
    fcap -->|"yes"| fesc["blocked: futile_rounds<br/>escalation NAMES the cause:<br/>quota, timeouts, reverts, signatures"]
    fcap -->|"no"| next

    classDef ok fill:#eafaf1,stroke:#27ae60
    classDef bad fill:#fdeaea,stroke:#c0392b
    class p,pesc ok
    class f,fesc bad
```

The diagnosis attached to an escalation walks a **closed list of mechanical
evidence**, most precise first (`reviewcycle._diagnose`, §5.7.0):

```mermaid
flowchart TD
    d["Escalation needs a cause"] --> s1{"frozen-signature violations >= 2?"}
    s1 -->|"yes"| o1["contract signatures keep drifting"]
    s1 -->|"no"| s2{"scope violations >= 2?"}
    s2 -->|"yes"| o2["executor keeps hitting the boundary"]
    s2 -->|"no"| s3{"executor failures >= 2?"}
    s3 -->|"yes"| o3["the executor cannot run here"]
    s3 -->|"no"| s4{"two reviewers disagreed on the SAME diff?"}
    s4 -->|"yes"| o4["reviewer spread, not task trouble"]
    s4 -->|"no"| s5{"wide front of findings that does not shrink:<br/>3+ categories, 3+ findings per round?"}
    s5 -->|"yes"| o5["size named as a HYPOTHESIS — split the task"]
    s5 -->|"no"| o6["report the TRAJECTORY of the last four rounds<br/>and say plainly: no mechanical cause found"]

    classDef out fill:#eef6ff,stroke:#3b82f6
    class o1,o2,o3,o4,o5,o6 out
```

The old fallback — "rounds exhausted, the task is probably too large" — was
wrong six times out of six on the frozen gold set, and each time it sent the
owner to split a task whose real trouble was elsewhere.

---

## 5. One round as a conversation

The same round as diagram 3, but showing **who holds what** at each moment.
Note where the two model calls sit and how little each of them is told.

```mermaid
sequenceDiagram
    autonumber
    participant H as Owner
    participant O as Orchestrator
    participant E as Executor
    participant G as Gate
    participant R as Reviewer
    participant M as Memory
    participant Git as git

    O->>Git: git status, rev-parse HEAD
    O->>G: baseline run on HEAD
    G-->>O: green — red would block the task at zero cost

    O->>M: retrieve lessons for this task
    M-->>O: up to 5 lessons, ~2000 chars
    O->>E: handoff = goal + task + acceptance + repo map + last findings + lessons
    Note over E: writes code inside allowed paths only —<br/>git is read-only, the orchestrator commits
    E-->>O: JSON report: status, summary, evidence, dispute?, deviations?

    O->>G: full suite — the orchestrator runs it, not the executor
    G-->>O: tail of output
    O->>Git: diff --name-only for scope and protected-test check
    O->>Git: work diff, long files condensed with sha256 and disclosure

    O->>R: rules + acceptance + human decisions + norms + DIFF + gate output
    Note over R: read-only tools, no Bash —<br/>may return verification_requests
    R-->>O: verdict: analysis, verdict, summary, findings, out_of_scope_notes

    opt verification enabled for this task
        O->>G: whitelisted kinds only — unittest, git_show, git_log, python
        G-->>O: sanitized output
        O->>R: second pass with the results
        R-->>O: refined verdict — failure keeps the FIRST verdict
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

## 6. The verdict contract — form, meaning, rescue

A verdict is the only thing that can close a task, so its failure modes get
their own machinery (§4.2, §4.2.1). Measured on E13: **5 reviewer calls out of
17 returned no valid verdict and burned 37 % of all review money.**

```mermaid
flowchart TD
    rcall["Reviewer call — claude -p --json-schema"] --> env{"Envelope contains structured output?"}

    env -->|"yes"| val
    env -->|"empty envelope"| salv["SALVAGE from the stream, §4.2.1<br/>take the last tool call carrying a full verdict,<br/>reassemble fields if the model tagged them"]
    salv --> salvok{"Recovered?"}
    salvok -->|"no"| fail
    salvok -->|"yes"| val

    val{"validate_verdict — MEANING, not just schema<br/>verdicts.verdict_problem"}
    val -->|"verdict outside approve / request_changes / blocked"| retry
    val -->|"severity or category outside the closed enum"| retry
    val -->|"approve while a blocker or major finding stands"| retry
    val -->|"request_changes or blocked with ZERO findings"| retry
    val -->|"analysis under 40 chars or summary under 20 — a stub"| retry
    val -->|"passes"| ok["Verdict accepted<br/>salvaged ones are marked as such<br/>in the journal and in metrics"]

    retry{"Retry allowed?"}
    retry -->|"deterministic refusal, budget_exhausted:<br/>the same prompt ends at the same place"| fail
    retry -->|"yes, exactly once"| again["Re-ask with THE REASON appended<br/>at the very TAIL of the prompt —<br/>in the stable prefix it would void<br/>the task's whole cache"]
    again --> rcall

    fail["review_failed, blocked: invalid_verdict<br/>stash + inbox question carrying the DIAGNOSIS.<br/>Evidence is never overwritten: a retried task<br/>writes its stream to a free name"]

    classDef bad fill:#fdeaea,stroke:#c0392b
    classDef good fill:#eafaf1,stroke:#27ae60
    class fail bad
    class ok good
```

Then `decide()` turns an accepted verdict into an outcome
(`verdicts.decide`), in this order:

```mermaid
flowchart TD
    v["Accepted verdict"] --> a{"verdict == approve?"}
    a -->|"yes"| cnt{"approvals on THIS diff_sha >= confirmations, default 2?"}
    cnt -->|"yes"| done["DONE, exit 0<br/>commit, status done"]
    cnt -->|"no"| cf["CONFIRM, exit 10<br/>re-review the SAME diff, executor not called;<br/>confirming rounds do not spend the fix limit"]
    a -->|"no"| b{"verdict == blocked?"}
    b -->|"yes"| em["ESCALATE_MAX, exit 20"]
    b -->|"no"| rounds{"productive rounds >= max_rounds?"}
    rounds -->|"yes"| em
    rounds -->|"no"| nc{"same finding categories AND count not shrinking,<br/>compared with the last NON-confirming round?"}
    nc -->|"yes"| en["ESCALATE_NONCONVERGENT, exit 20<br/>needs a diagnosis, not another lap"]
    nc -->|"no"| it{"any finding about INTENT —<br/>architecture, scope, or an explicit intent flag?"}
    it -->|"yes"| au["ASK_USER, exit 11<br/>question to the owner, mechanical findings noted"]
    it -->|"no"| cont["CONTINUE, exit 10<br/>mechanical findings back to the executor"]

    classDef good fill:#eafaf1,stroke:#27ae60
    classDef ask fill:#fef9e7,stroke:#b7950b
    classDef bad fill:#fdeaea,stroke:#c0392b
    class done good
    class au,cf ask
    class em,en bad
```

Approvals are counted **per diff hash**, not per task: an approve earned
before a later fix belongs to a different diff and does not count. Without
that rule a chain of approve → flip → fix → approve closed tasks whose final
code exactly one reviewer had ever seen.

Between rounds the loop also keeps the **best** round (fewest findings) and
rolls back to it if the next one is worse — but never on a confirming round,
where a change in finding count is reviewer spread, not regression.

---

## 7. Context flow — who is told what, and in which order

Context is a budget, not a courtesy (§8). Two rules shape every prompt: the
**asymmetry** that keeps the reviewer independent, and the **cache prefix**
that decides what a call costs.

```mermaid
flowchart LR
    subgraph src["Sources"]
        goal["goal + task spec + acceptance<br/>from tasks.json"]
        map["repo map — ranked public signatures, ~25 lines<br/>codemap / pyindex / tsindex;<br/>NOT attached for local edits"]
        fb["last verdict's MECHANICAL findings"]
        les["lessons from memory, E9"]
        hum["owner's decision for this task"]
        diff["git diff — files over ~400 lines condensed<br/>to name, +A/-B, sha256, excerpt,<br/>with the hiding DECLARED"]
        tests["gate output"]
    end

    subgraph ex["EXECUTOR prompt"]
        e1["goal · lessons · task + acceptance ·<br/>repo map · feedback · constraints ·<br/>dispute channel · output schema"]
    end

    subgraph rv["REVIEWER prompt — cut on cache boundaries"]
        r1["1 · rules — stable across ALL tasks:<br/>review rules, injection rule, language, lens"]
        r2["2 · task — stable within the task:<br/>acceptance, owner's decisions, repo norms"]
        r3["3 · tail — volatile:<br/>DIFF, then gate output,<br/>then verification and retry notes"]
    end

    goal --> e1
    map --> e1
    fb --> e1
    les --> e1
    hum --> e1

    goal --> r2
    hum --> r2
    les --> r2
    diff --> r3
    tests --> r3

    e1 -.->|"NEVER forwarded"| rv
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

## 8. Where the human is asked, and how the answer travels back

The loop is an autopilot with a **named** list of escalations (§1.1). The
scarcest resource in the system is the owner's attention, so the triage rule
is explicit: of 6–7 findings per task, 1–2 reach the human (§5.7).

```mermaid
flowchart TD
    subgraph out["Terminal outcomes that call the human"]
        o1["ask_user — findings about INTENT"]
        o2["dispute — executor cannot finish honestly"]
        o3["invalid_verdict — review never happened"]
        o4["escalate_max / nonconvergent — with a diagnosis"]
        o5["futile_rounds — the environment burned the rounds"]
        o6["baseline_red · integrity · crash · budget"]
    end

    o1 --> ask
    o2 --> ask
    o3 --> ask
    o4 --> ask
    o5 --> ask
    o6 --> ask

    ask["state.ask — question qNNN in .swarm/state.json<br/>carries diagnosis, findings, round, stash id.<br/>Work is stashed, never discarded"]
    ask --> surf

    subgraph surf["Human-facing surfaces, §9.3 — one event, one name: vocab.py"]
        b["swarm board — one-page run board, refreshed each round"]
        i["swarm inbox — the open questions"]
        w["swarm why TASK — why it stopped, what to do next"]
        rep["swarm report — the run as connected prose"]
        ab["swarm ab — arms of the measurement"]
    end

    surf --> act{"Owner decides"}

    act -->|"swarm answer qNNN --add-path"| ans["human_answer written to the task.<br/>Binding for the WHOLE task, and shown to<br/>the REVIEWER as decisions not to be disputed"]
    act -->|"swarm policy add"| pol["run-level suppression: matching non-blocker<br/>findings are filtered by the orchestrator,<br/>the reviewer stays sighted"]
    act -->|"swarm replan TASK"| rp["planner emits a plan-diff: split the task,<br/>widen paths, or carve the objection<br/>into a new task of the right type"]
    act -->|"swarm retry TASK"| rt["blocked to pending, round numbering restarts,<br/>prior evidence kept under its own name"]

    ans --> queue[("queue continues: swarm go")]
    pol --> queue
    rp --> queue
    rt --> queue

    classDef ask fill:#fef9e7,stroke:#b7950b
    class ask,act ask
```

The rule the pilot paid for: **an escalation must carry a diagnosis.** A task
that goes to `blocked` with no journal entry and no question leaves the
operator blind — the work may have been finished and green, with only the
review having failed (§4.2).

---

## 9. Memory between runs (E9)

Memory is an **observation**, not part of the work: its failure leaves a trace
and never costs a run. Every path degrades — Postgres to a local file scan,
retrieval to nothing at all.

```mermaid
flowchart LR
    task["Task reaches a terminal outcome"] --> rec["record_task_outcome<br/>one lesson, body capped at 700 chars,<br/>outcome: useful | dead_end | corrected"]
    rec --> files[".swarm/memory/*.md — files are the truth"]
    files --> pgi["PostgreSQL swarm_memory<br/>FTS index + embedding vectors"]

    runend["Run ends"] --> refl["reflect_after_run<br/>cheap helper consolidates the digest"]
    refl --> less["LESSONS.md — the digest a human reads"]
    refl --> pgi

    q["New task starts, or a reviewer prompt is built"] --> ret["retrieve: FTS in PG, then local file scan<br/>top-5, ~2000-char budget<br/>NEVER raises: failure = empty list"]
    pgi -.-> ret
    files -.-> ret
    ret --> inj["injected as a lessons block into the<br/>executor handoff, and as repo norms<br/>into the reviewer prompt"]

    classDef store fill:#fff7e6,stroke:#d19a00
    class files,pgi,less store
```

Measured effect (E9, planner arm): with memory, a replan folded all three of
the owner's decisions into named tasks, including a satellite inherited from
a different dispute; without memory that satellite was lost in every replan.

---

## 10. Failure ladder: quota, crashes, liveness

Every one of these branches exists because the loop once mistook one kind of
failure for another and charged the wrong party (§5.2, §5.3).

```mermaid
flowchart TD
    acall["Any agent call"] --> res{"What came back?"}

    res -->|"silence over 600 s"| live{"Process alive AND stream moving?"}
    live -->|"yes"| wait["keep waiting — silence is not death"]
    live -->|"no"| restart["kill, git stash push -u, clean restart.<br/>Counts as a round. Two crashes in a row = blocked"]

    res -->|"wall clock over 30 min"| restart

    res -->|"session / rate / usage limit, quota, 429"| quota
    res -->|"HTTP 403 with ZERO work done: no tokens, no cost"| quota
    res -->|"HTTP 403 with tokens actually SPENT"| notquota["NOT quota — the provider did work.<br/>That refusal is investigated, not waited out"]

    quota["QUOTA PAUSE — not an agent error, not a bad verdict.<br/>Backoff ladder 1 to 2 to 4 min; quota_wait_s in metrics"]
    quota --> auto{"quota_resume == auto?"}
    auto -->|"no, the default"| exit4["work to stash · task back to pending ·<br/>run exits with code 4 — the human decides when to resume"]
    auto -->|"yes"| parse["parse the reset time out of the provider's own message,<br/>sleep past it plus 90 s, continue the queue"]
    parse --> guard{"within quota_resume_max, 3,<br/>and quota_resume_max_wait_s, 6 h?"}
    guard -->|"no"| exit4
    guard -->|"yes"| cont["resume — every resume is a journal event"]

    res -->|"process dies in seconds, empty stream, twice"| unavail["executor_unavailable:<br/>task back to pending, run stops.<br/>The next task would die the same way"]

    classDef bad fill:#fdeaea,stroke:#c0392b
    classDef warn fill:#fef9e7,stroke:#b7950b
    class exit4,unavail,notquota bad
    class quota,restart warn
```

The same defect class — "a refusal by quota looks like a bad answer" — has
been fixed three times at three different levels. It is worth reading the
branches above as scar tissue, not as configuration.

---

## 11. CLI surface

One entry point, `swarm`; the commands map onto the subsystems above (§9.2).

```mermaid
flowchart LR
    subgraph run["Running"]
        c1["go — plan and execute, end to end"]
        c2["run / resume — execute the existing queue"]
        c3["plan / replan — planner only"]
        c4["retry — return a blocked task"]
    end
    subgraph look["Looking"]
        c5["board · status — where the run is"]
        c6["why — why this task stopped"]
        c7["report — the run as prose"]
        c8["ab — arms of the measurement"]
        c9["map · impact — repo symbols and callers"]
    end
    subgraph decide["Deciding"]
        c10["inbox · answer — the human channel"]
        c11["policy — run-level finding suppression"]
    end
    subgraph mem["Memory"]
        c12["memory add / search / show / forget"]
        c13["memory reflect / reindex / sync"]
    end
    c14["doctor — is the environment fit to run"]

    run --> state[(".swarm/ and git")]
    look --> state
    decide --> state
    mem --> pg[("swarm_memory")]
```

---

## Change log

### v0.1 (2026-08-23)

First edition. Eleven Mermaid views drawn from `05-agent-swarm.md` v0.50 and
from the implementation in `tools/swarm/swarm/` (`loop.run_task`,
`verdicts.decide`, `gitops`, `reviewer`, `promptbuilder`, `helpers`,
`meminject`). Written in English per the owner's standing rule for new
documents; the surrounding Russian documents are untouched.
