# Project planning for large builds

How to plan and execute a project too big to hold in your head — or in
one chat session. The method optimizes for one thing: at every moment,
there is a working system, a trusted record of where you are, and an
obvious next step. Written to be followed by humans and agents alike;
smaller models especially should follow it mechanically.

## The core moves

1. **Define done as observable behavior.** "The store survives a node
   failure without losing acked writes" — testable. "Implement raft" —
   not done-able. Every goal on the plan is phrased as something you
   can DEMONSTRATE.
2. **Slice into milestones that are each a working system.** Not
   layers ("write all the storage code, then all the network code") —
   vertical slices, each usable and testable end-to-end, each building
   on the last. A milestone that can't run is two milestones.
3. **Give every milestone an executable gate.** The gate is a test or
   checkable procedure written down BEFORE the work: "gate: kill -9
   during writes, restart, state equals a prefix of acked writes."
   Gates stay in CI forever — they are how earlier milestones stay
   trusted while later ones churn.
4. **Order by risk and dependency, not by ease.** The riskiest
   load-bearing unknown goes as early as dependencies allow — if the
   hard part fails, you want a small sunk cost and a working smaller
   system to fall back to.
5. **One milestone in flight.** If a completed milestone's gate breaks,
   fixing it preempts new work. The plan's value is that finished
   things STAY finished.

## The plan document

Keep exactly two living files in the repo, and keep them short:

**PLAN.md** — the ladder. For each milestone: a name, 2–5 sentences of
scope, the gate, and any decided-but-not-obvious design choices (with
one line of why). Not a spec — links to deeper docs when needed.

**STATUS.md** — the resume point. Rewrite it (don't append) every time
a gate passes or work stops mid-milestone:

```markdown
# Status — updated 2026-08-09
Current milestone: M4 (raft snapshots + membership)
Done: M1–M3 (gates green in CI)
In progress: snapshot install on follower restart — sender side done,
  receiver applies but doesn't yet truncate its log.
Next single step: truncate receiver log after snapshot apply; then the
  wiped-node-rejoin gate.
Landmines: MemoryStorage must get ApplySnapshot BEFORE the engine
  restore or FirstIndex panics (found the hard way).
```

Anyone — a returning human, a fresh agent session — reads STATUS.md
and continues without archaeology. This is the single highest-leverage
habit for long projects; update it at every stopping point, no
exceptions.

## Walking a milestone reliably

Within a milestone, the same shape repeats at small scale:

1. Write (or update) the gate test first — red.
2. List the sub-steps to green; pick the smallest that leaves the
   build compiling and tests passing.
3. Do it; commit with a message naming the step.
4. Repeat until the gate is green; then update STATUS.md and PLAN.md
   (check the milestone off; record any design decisions made along
   the way).

Rules while walking:

- **Never two broken things at once.** If step N revealed a bug in an
  earlier layer, finish or stash step N, fix the layer under its own
  test, then resume.
- **Timebox investigations.** When stuck > ~2 focused attempts, write
  down what was ruled out (in STATUS.md landmines), and either change
  approach or descend one level (write a smaller test that isolates
  the confusion).
- **No drive-by refactors.** Note them in PLAN.md's backlog; a
  milestone touches what its scope names.
- **Cut scope, not verification.** Under pressure, shrink the
  milestone ("split without dual-write window; conflicts flagged
  manually") — never skip its gate. A smaller verified system beats a
  larger rumored one.

## Re-planning

Plans age. When reality disagrees with PLAN.md:

- Small drift (a milestone splits, a step moves) — edit PLAN.md in the
  same commit as the work; note nothing else.
- A real discovery (chosen library can't do X, design assumption
  false) — stop, write the discovery and options in PLAN.md as a
  short decision record (context, options, choice, why), THEN
  continue. Decisions made invisibly get re-litigated forever.
- Every few milestones, reread the whole PLAN.md tail: milestones
  written months ago often describe a system you no longer need —
  delete freely; the ladder only ever has to be right about the next
  rung and honest about the destination.

## Estimating without lying

Give ranges tied to the ladder ("M3 is the risky one; M1–M2 days,
M3 one to three weeks") and re-estimate at each gate — gates are the
only moments you have new information worth updating on. A slipping
gate date is information, not failure; hiding it by skipping the gate
converts a schedule problem into a correctness problem.
