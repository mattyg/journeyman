# CAD-from-reference agent harness (FreeCAD / feature-tree edition)

A staged prompt harness for an agent that produces a parametric FreeCAD model
from a text description plus reference images, following a human designer's
feature-tree workflow.

Two core principles:

1. **The deliverable is renderable geometry, not prose.** Prose specs are
   unfalsifiable; a rendered silhouette next to a reference silhouette is not.
2. **Verify per feature, not per model.** A designer looks at the screen after
   every operation. The agent must too. This localises errors to a single
   feature instead of burying them in a finished body.

---

## Stage 0 — Input contract

Reject or flag the task if these are missing. Do not proceed on inference.

| Field | Required | Notes |
|---|---|---|
| `target_scale` | yes | Overall size or a named fastener (e.g. "fits M3") |
| `material_thickness` | yes | Or "derive from function" plus the function |
| `fidelity_target` | yes | `replica` \| `stylised` \| `functional-analogue` |
| `intended_use` | yes | Load-bearing, display, toy, print-in-place, etc. |
| `freecad_version` | yes | 1.0+ has materially better topological naming |
| `workbench` | yes | `PartDesign` \| `Part` \| `SheetMetal` (addon) |
| `parameter_source` | yes | Spreadsheet object, or named constraints |
| `reference_images` | no | With per-image view type (Stage 1) |
| `features_to_ignore` | no | Surface detail, lettering, branding |
| `unseen_features` | no | Anything not visible in the references |

`fidelity_target` is the highest-leverage field and the most often omitted.
`replica` and `stylised` produce different models from identical images, and an
agent with no signal defaults to `replica` because that is what images imply.

### User input template

```
OBJECT: <one line>
FIDELITY: replica | stylised | functional-analogue
USE: <what it is for; state if non-functional>
SCALE: <overall dimension or fastener spec>
THICKNESS: <mm>
FREECAD: <version>   WORKBENCH: <PartDesign|Part|SheetMetal>
PARAMS: spreadsheet | named-constraints
IGNORE: <features present in refs that should not be modelled>
UNSEEN: <features not visible in any ref>
IMAGES:
  - path: ...    view: front-ortho | side-ortho | three-quarter | detail
    rectified: yes/no    scale_ref: yes/no
```

---

## Stage 1 — Image triage

For each supplied image the agent records, before using it:

- **View type.** Orthographic front / orthographic side / three-quarter / detail.
- **Foreshortening risk.** Any non-orthographic view of a planar face is
  unreliable for aspect ratio and curvature. Flag it.
- **Specularity risk.** Polished or reflective subjects produce highlight lines
  that read as edges. Flag it.
- **Scale reference present?** yes/no.

Preprocessing, in descending order of value:

1. **Perspective rectification** to true orthographic. A known-circular hole is
   sufficient to recover the plane's orientation. Bent parts have two planes —
   rectify each separately and produce two images.
2. **Silhouette extraction** (threshold to flat black-on-white). Removes
   specular pseudo-edges. Makes convexity visually checkable.
3. **Scale calibration** — in-frame ruler, or grid overlay after rectification.

Do **not** apply generative upscaling or inpainting. Invented contours are worse
than absent ones because they are indistinguishable from real ones downstream.

Limits to encode explicitly: preprocessing cannot synthesise a view that was not
shot, cannot reveal internal geometry, and cannot reveal parts absent from an
assembled photograph.

---

## Stage 2 — Assumption ledger (gate)

Before any geometry, the agent emits:

```
| # | Assumption | Source | Confidence | If wrong |
|---|-----------|--------|-----------|----------|
| 1 | Outline is strictly convex | silhouette | med | outline wrong throughout |
| 2 | Bolt hole Ø3.5 (M3) | stated scale | high | all hole/boss features rescale |
| 3 | Bend is 90° | side-ortho | low | part function changes entirely |
```

Rules:

- Every numeric value not given in the input contract is an assumption.
- Sort by `If wrong` severity, not by confidence.
- Then ask **at most 3** questions, drawn only from rows where confidence is low
  **and** consequence is high.
- Questions must be answerable in one word or number. No open prompts.

Cost asymmetry: an assumption corrected here costs one line; the same assumption
corrected after Stage 5 costs a tree rebuild.

---

## Stage 3 — Feature tree plan (gate)

The distinguishing stage of this edition. The agent plans the entire tree before
drawing a single sketch, and the plan is reviewed before building.

### 3a — Datum skeleton

Declare origin planes and any additional datum planes/axes/points first. **All
sketches attach to datums, never to model faces.** This is the single most
effective mitigation for FreeCAD's topological naming problem.

### 3b — Sketch inventory

For each planned sketch: name, host datum, what it defines, and its intended
degrees of freedom (always 0).

### 3c — Ordered feature list

```
| # | Feature | Type | Depends on | Sketch | Notes |
|---|---------|------|-----------|--------|-------|
| 1 | Body outline | Pad | Datum XY | Sk_Outline | 2 mm |
| 2 | Aperture | Pocket (through) | 1 | Sk_Aperture | |
| 3 | Bolt hole | Pocket (through) | 1 | Sk_BoltHole | Ø3.5 |
| 4 | Collar boss | Pad | 1, 3 | Sk_Collar | additive after cut — dependency |
| 5 | Edge breaks | Fillet | 1–4 | — | finishing, last |
```

**Ordering rule.** Order by dependency. Where two features are mutually
independent, prefer additive before subtractive. Do **not** enforce strict
category blocks — a boss on a face that only exists after a pocket must follow
that pocket, and forcing otherwise produces contorted sketches or silent rule
violations. State the reason whenever category order is broken (feature 4 above).

**Finishing last is non-negotiable.** Fillets and chamfers reference edges by
generated name; those names reshuffle when upstream features rebuild. A fillet
placed mid-tree is the most common cause of a model that cannot be
re-parameterised.

### 3d — Fork declarations

Name any modelling decision where FreeCAD offers no direct feature, and commit
to an approach before building. Common forks:

- **Bends.** PartDesign has no native bend. Choose: SheetMetal addon workbench /
  model the bent form directly as a sweep or loft / build flat and apply a
  separate bend operation. This determines whether a flat pattern exists at all.
- **Organic blends.** Additive loft vs. large-radius fillet vs. surface
  workbench.
- **Multi-body assemblies.** One Body per solid; declare the count now.

### 3e — Parameter schema

A FreeCAD spreadsheet with aliased cells, or named constraints. Two or three
drivers; everything else derived.

```
size        = 1.8      // linear scale multiplier
thickness   = 2.0      // mm
hole_d      = 3.5      // mm — drives collar OD, cap bore
```

Rule: if a dimension can be derived from another, derive it. Hard-coded values
that should be derived are the main source of models that break under scaling.

---

## Stage 4 — Incremental build with per-feature verification

Build one feature at a time. After **each** feature:

1. Recompute the document. Abort on any error or touched-object warning.
2. Assert the sketch has **0 degrees of freedom** (if the feature has a sketch).
3. Render an orthographic view.
4. Check the running invariants that are meaningful at this point (bounding box
   after the first pad, hole diameter after a pocket, etc.).
5. Log: feature name, resulting solid count, bounding box, recompute status.

Do not proceed to feature *n+1* while feature *n* fails. Cap retries per feature
at 2, then stop and report rather than accumulating damage.

Build constraints to state in the prompt:

- Parameters live in the spreadsheet; geometry references them by alias.
- No numeric literals in feature definitions.
- Every sketch fully constrained before it is used.
- No feature may reference a face, edge, or vertex by generated name.
- Comment each feature with its correspondence to the reference.
- Features derived from `unseen_features` rather than an image are marked
  `INFERRED`.

---

## Stage 5 — Whole-model verification

1. Render orthographic front, side, top.
2. Extract the silhouette of the rendered front view.
3. Place it beside the rectified reference silhouette at matched scale.
4. Compute and report all invariants below.
5. On failure, revise and repeat. Cap at 3 iterations, then report the
   unresolved failure rather than continuing.

### Geometry invariants

| Check | Method | Fails if |
|---|---|---|
| Bounding box | measure shape | >5% from stated scale |
| Silhouette IoU | rendered vs rectified ref | <0.90 `replica`, <0.75 `stylised` |
| Hole diameters | measure | inconsistent with stated fastener |
| Wall thickness | min section | below print/machining minimum |
| Convexity | hull area vs actual | mismatch when plan implies convex |
| Manifold | `Shape.isValid()`, watertight | invalid or open |
| Solid count | count solids in body | differs from declared count |

### Feature-tree invariants (FreeCAD-specific)

| Check | Method | Fails if |
|---|---|---|
| Sketch constraint fullness | DoF per sketch | any sketch DoF > 0 |
| Recompute integrity | full document recompute | any error or touched object |
| Datum attachment | inspect sketch supports | any sketch attached to a model face |
| Named-reference audit | inspect feature refs | any reference to a generated name |
| Finishing position | tree order | any fillet/chamfer before a boolean |
| Parametric resilience | rebuild at 0.5×, 2×, and ±10% on each driver | recompute error or self-intersection |
| Tree legibility | count features, check naming | unnamed features, or count far above plan |

`Parametric resilience` is the topological-naming canary. It is cheap and it
catches the failure mode that makes a model useless three weeks later.

Report every invariant numerically, passing or failing. A model that reports
IoU 0.71 honestly is more useful than one that claims success.

---

## Stage 6 — Output

- The FreeCAD document, plus the generating Python script.
- The feature tree as built, diffed against the Stage 3 plan, with any deviation
  explained.
- Rendered orthographic views.
- The invariant tables with measured values.
- The assumption ledger, updated — assumptions confirmed by the build promoted,
  those still unverified left flagged.
- A residual-uncertainty list: what a human should check before committing.

---

## Failure modes to guard explicitly

**Topological naming breakage.** The dominant FreeCAD failure. Mitigations are
all in the harness above: datum-attached sketches, no generated-name references,
finishing last, parametric resilience test. Restate them at build time, not only
at planning time.

**Under-constrained sketches.** The sketch equivalent of a magic number: the
model looks right and moves unpredictably. Machine-checkable, so check it.

**Plan/build drift.** The agent produces a clean Stage 3 plan then builds
something else. The Stage 6 tree diff exists to catch this. Treat any unexplained
deviation as a failure even if the geometry passes.

**Drift under correction.** When told "this is wrong," the agent must name the
specific feature or parameter it is changing and re-run all invariants, not only
the one that prompted the change. Otherwise repeated correction becomes a random
walk.

**Reference over-fitting.** With `fidelity_target: stylised`, more reference
images make output worse — they pull toward surface detail the target discards.
Cap image count for stylised tasks, or weight the description above the images.

**Silent gap-filling.** Any feature the agent cannot see must be surfaced, not
invented. *"If a feature is required by the description but not visible in any
reference, model it and mark it `INFERRED`. Never model an inferred feature
silently."*

**Scale anchoring.** An agent that finds a dimension in an adjacent source (a
product listing, a standard) will anchor on it even when the task states a
different scale. *"`target_scale` overrides any dimension found in reference
material."*

**Category-order rigidity.** Over-enforcing "additives before removals" produces
contorted sketches or unreported violations. Dependency order governs; category
order is a tiebreak only.

---

## Evaluating the harness

Build a golden set from parts where you have both the source model and
photographs. Feed the agent only the description and images; score against the
known model.

Metrics, in rough order of usefulness:

- **Silhouette IoU** on rectified front and side views. Primary shape metric.
- **Bounding-box error** (%). Catches scale anchoring.
- **Feature recall** — fraction of distinct features present. Catches omission.
- **Feature precision** — fraction of modelled features that exist in ground
  truth. Catches invention.
- **Assumption calibration** — of assumptions marked high-confidence, what
  fraction were correct? Miscalibration predicts every other failure. If you
  instrument one thing, instrument this.
- **Tree edit distance** to the reference tree. Measures whether the *process*
  matched, not only the result — the point of this edition.
- **Parametric survival rate** — fraction of golden-set models that still
  recompute after a ±10% perturbation of every driver.
- **Questions-to-convergence** — clarification rounds needed to reach threshold.

Include at least one case where the target deliberately departs from its
references (a stylised toy from photos of the real object). Agents that score
well on replica tasks often fail these badly, and the failure is invisible
without such a case in the set.

---

## Caveat

The stage structure generalises from a post-mortem of a single failed attempt;
the FreeCAD-specific invariants generalise from known properties of the tool.
Treat the structure as sound and the specific thresholds — IoU 0.90, 3 questions,
2 retries per feature, ±10% perturbation — as starting values to tune against
your golden set.
