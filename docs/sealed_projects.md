# Sealed projects and the program contract (GOAL 18.2, 18.6; UPGRADES N7, N8)

## Seal at project start

    chh lab seal PROJECT --data FILE --label-column C --rows N [--parts 2] --beacon-round R --beacon VALUE --provenance private|public
    chh lab start AGENT --project PROJECT ...
    chh lab promote SESSION --request promote.yaml
    chh court register-sealed PROMOTION --project PROJECT [--revalidate]
    chh approve-role TARGET --approver NAME     (HUMAN)
    chh court run CASE --docker

kerneld reads the file; the host only names it. The split comes from `draw("seal_split", <sha256 of the data>, <round:value>)`,
so nobody picks the rows. The Lab's part is public DEV data; each sealed part is an EXTERNAL target with private labels.
A sealed row whose features also occur in the Lab's part or an earlier sealed part is dropped.

**"Sealed" is a ledger fact.** Each part's `register_dataset` entry and each session's `register_artifact` entry (kind
`lab_session`) sit in one hash chain. A target is `sealed-at-start` only if every session of the project comes after the
seal and the case's own session is one of them. Otherwise it is `seal-after-lab`, which is never fresh. No Kernel core
change was needed: both entry kinds already exist.

**Parts.** One part gives VALIDATED. `--revalidate` fixes part 2 as the second test before any label of part 1 is read,
which gives RE-VALIDATED under the existing rules: same claim core, same frozen code, disjoint rows.

**Provenance.** A seal states where the data comes from, and there is no default. Sealing a copy of a public dataset
hides nothing from a networked Lab, so `--provenance public` caps the claim as RETROSPECTIVE. Only a stated `private`
escapes the cap, and a seal from before this statement existed is capped too.

**Not shown by the seal.** That nobody gave the raw file to a Lab by another route, and that data stated `private` really
is. Both rest on the researcher: this catches a mistake and makes a false statement an explicit one, but it cannot catch
a lie. They are listed as assumptions on the claim. Near-duplicate rows are not dropped, only rows with identical
features. The human role approval of each part still applies.

Part size is the researcher's choice today; the power check (docs/court_rehearsal.md) shows what a size can detect.

## The program contract

The `build_model(seed)` wrapper covers tabular fit and predict. `contract: program` in the promotion request covers the
rest of what can run offline:

    $CHH_INPUTS/train.csv     training rows with a `label` column
    $CHH_INPUTS/test.csv      test rows without one
    $CHH_OUTPUTS              the program writes predictions.csv: header `prediction`, one row per test row
    $CHH_SEED                 the seed

`method` is the entry file, `method_files` any further text files; the baseline likewise. They are composed into one
Python program, as readable string literals followed by one shared shim, and go through the same Kernel route: K3
re-executes them, and kerneld computes the metric from the predictions. Anything the program prints or reports is
ignored, so Lab output can never mint a certificate.

Limits: the Court is offline, so a program that needs a network or a model API at run time fails as COULD NOT RUN. Output
must be one prediction per test row with one of the existing metrics. Text files only. Packages come from the candidate
image. The Claim Audit route still takes `build_model` only. Replication (`chh court replicate`) takes `build_model` arms
on minted cases only.
