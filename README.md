# Festival-Manager-Project


`Admin (external) → Gen Sec / FCs → Heads → AHs → Volunteers`

---

## Admin

Not a platform user. They communicate with FCs only — either directly to an FC or to all FCs. When that information enters the platform, it gets tagged as admin-originated so downstream people know this isn't an FC decision, it's an external constraint. That distinction matters because the response options are different — you can push back on an FC call, you generally can't on an admin directive.

---

## Gen Sec

Sees everything. Can broadcast to all tiers simultaneously. Receives escalations and strategic reports from FCs. The only user who can trigger a fest-wide verified notice. In practice probably the least operationally active user on the platform during the fest itself, but the most important for strategic overrides and emergencies.

---

## Festival Chiefs (FCs)

This is where the architecture gets more nuanced than the original doc assumed. FCs divide verticals among themselves by mutual discussion each edition — so FC 1 might own Logistics, Events, and Hospitality while FC 2 owns PR, ATS, and Sponsorship. This assignment needs to be configured at the start of each fest edition and drives the entire permission structure below it.

An FC communicates downward only to their assigned Heads, not to all Heads. They communicate laterally with other FCs for cross-FC coordination. They communicate upward to Gen Sec for escalations. They are also the relay point for admin information; they can either pass it through their Heads or broadcast it directly, but the platform should make the origin visible either way.

An FC cannot contact Heads outside their assigned verticals directly. That would be hierarchy bypassing.

---

## Heads

The most operationally critical role. They own the verified information pipeline for their vertical — they are the only ones who can publish or update official information. They communicate downward to their AHs, upward to their assigned FC only, and laterally to other Heads for cross-vertical coordination.

They need full visibility into their entire vertical — every AH, every task status, every volunteer workload. The Head's dashboard is the most important screen in the platform because the primary breakdown identified is within vertical coordination, and Heads are where that gets resolved or missed.

When info changes mid-fest, the Head is responsible for pushing the update, and the platform should show them exactly who has and hasn't acknowledged it.

---

## Associate Heads (AHs)

Receivers during planning, executors during the fest. They get directives from their Head and translate them into tasks for volunteers. They coordinate laterally with AHs from other verticals during execution, and it's relationship-dependent right now, which is the problem. The platform needs to make cross-vertical AH coordination a structured channel, not an informal one.

AHs see their own volunteer workload, pending tasks, and vertical notices. They cannot contact Heads of other verticals directly, cannot publish verified info, and cannot contact FCs. Their key new capability the platform adds: when a volunteer flags uncertainty instead of guessing, the AH gets a structured prompt to resolve it and push the answer back down as a verified response.

---

## Volunteers

The most constrained role and the one with the most to gain from the platform. Right now they receive information through informal channels and are expected to answer participant questions even when unsure. The platform does two things for them: gives them a single source of truth they can reference confidently, and gives them a way to flag "I don't know" upward to their AH instead of guessing.

They see only: their assigned tasks, verified notices relevant to their vertical, and updates their AH pushes down. They cannot contact anyone outside their AH. They cannot assign tasks. They cannot publish anything.

The "I don't know" flag is not a minor feature — it's the direct fix for the accommodation incident and every incident like it.

---

## The three communication types

**Hierarchical** — delegation going down, escalation going up. This is the primary channel. Every role communicates vertically to their immediate neighbor in the chain. Skipping levels is not permitted except in declared emergencies, which is a separate mode.

**Lateral** — cross-vertical coordination. Permitted at three levels: FC↔FC, Head↔Head, and AH↔AH. Not permitted below AH level — volunteers do not coordinate cross-vertically, they escalate to their AH who handles it. Lateral channels are scoped — an AH can only initiate lateral contact with AHs from other verticals, not with their Heads, not with FCs.

**Broadcast** — official notices pushed down from a specific tier. Gen Sec can broadcast to everyone. FCs can broadcast to their assigned verticals. Heads can broadcast within their vertical. AHs can push to their volunteers. Nobody below Head level can initiate a broadcast. Every broadcast is timestamped and attributed.



