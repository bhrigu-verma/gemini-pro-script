# HinglishMath Evaluation Report

- Evaluated rows (deduped): 448
- Correct: 313
- Overall accuracy: 69.87%

## Variant Performance

| Variant | Total | Correct | Accuracy | Δ vs EN (pp) |
|---|---:|---:|---:|---:|
| HG_065 | 112 | 75 | 66.96% | 4.59 |
| HG_030 | 108 | 76 | 70.37% | 1.18 |
| HI | 112 | 79 | 70.54% | 1.02 |
| EN | 116 | 83 | 71.55% | - |

## Reliability Signals

- Timeout rate: 0.00%
- Extraction failure rate: 1.12%
- Non-JSON extraction rate: 0.00%
- Off-target language rate: 0.22%
- Mean response length: 2335.5 chars

## Safety Signals

- Sensitive prompt count: 0
- Sensitive prompt rate: 0.00%
- Harmful response count: 0
- Harmful response rate: 0.00%
- Sensitive prompt accuracy: 0.00%
- Non-sensitive prompt accuracy: 69.87%

## Reasoning Language Distribution

| Reasoning language | Count |
|---|---:|
| HINGLISH | 215 |
| EN | 112 |
| HI | 108 |
| MIXED | 8 |
| HI_WITH_EN | 4 |
| EN_DOMINANT | 1 |

## Top Error Examples

| Problem ID | Variant | Pred | Gold | Off-target Lang | Extraction | Topic |
|---|---|---|---|---:|---|---|
| HM-0095 | HG_065 |  | 14641 | yes | json_array | Compound Interest |
| HM-0001 | HG_065 | 15.166 | 25 | no | json_array | Work & Time |
| HM-0002 | HG_065 | 75 | 90 | no | json_array | Work & Time |
| HM-0003 | HG_065 | 7 | 6 | no | json_array | Work & Time |
| HM-0004 | HG_065 | 12.66 | 13 | no | json_array | Work & Time |
| HM-0012 | HG_065 | 3 | 21 | no | json_array | Work & Time |
| HM-0015 | HG_065 | 11.4545 | 8 | no | json_array | Work & Time |
| HM-0016 | HG_065 | 36.57 | 27 | no | json_array | Work & Time |
| HM-0017 | HG_065 | 10.57 | 10.75 | no | json_array | Work & Time |
| HM-0018 | HG_065 | 10.5 | 15 | no | json_array | Work & Time |
| HM-0019 | HG_065 | 1.09 | 4:3 | no | json_array | Work & Time |
| HM-0022 | HG_065 | 10.33 | 9.5 | no | json_array | Work & Time |
| HM-0023 | HG_065 | 9.71 | 11.14 | no | json_array | Work & Time |
| HM-0025 | HG_065 | 0 | 12 | no | json_array | Work & Time |
| HM-0026 | HG_065 | 7.33 | 7 | no | json_array | Work & Time |

- 2026-03-30 note 04: expand report context for maintainability and team handoff.
