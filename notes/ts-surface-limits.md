# TS export surface — known unhandled shapes (log-and-fix)

The TS/JS import surface (`ts_surface.py`, catch 6/6b) is a regex-grade
extractor, not a TypeScript parser. It is ground-truthed against real
runtime export keys — 52/52 on ufo's `src/index.ts`, 4/4 on ohash's
built dist, zero false positives on either — so it is sound on the
export shapes real repos actually ship.

Self-review (PR #7) then pushed it past real-world shapes into exotic
re-export corners it does not yet handle. These are logged here rather
than fixed, because the extractor is an *aid* to the proposer, not a
verdict path: an imperfect surface can only weaken a proposal, never
manufacture a false gap. The gate's guarantee is unaffected. Deferred
by the same rule the ROADMAP uses elsewhere — document the limit, don't
pretend it's closed.

Known unhandled shapes, found by the gate, not yet handled:

- `export * from './x'` where `./x` itself has a default export: the
  star may forward `default` when it should not (a namespace star
  forwards named exports only, never the source's default).
- A named re-export of another module's default under an alias
  (`export { default as Name } from './x'`): default-origin counting is
  not fully correct across a resolved hop.
- Inline `type` modifiers inside a mixed brace re-export
  (`export { a, type B } from './x'`) in combination with a resolved
  source: the type/value split is right for the common cases and can
  miscount in nested combinations.

Not in this list (already handled and regression-tested): unresolved
relative named re-exports (defect 27), unresolved namespace star
re-exports (defect 30), ambient `declare namespace` as type-only
(defect 31), aliased merge into the host header (defect 28), trailing
newline (defect 29).

When these are picked up: same discipline as catch 6 — ground-truth
each shape against a real repo that ships it before claiming it, and
add a regression row per shape.
