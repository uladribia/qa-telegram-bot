# Clef-Flash post-retrieval decisions

- revision: `68a537e37062e8cce395419d072096da3301e5e4`
- model: @cf/cloudflare/clef-flash, one request per case, no fallback
- cases: held-out scored, calibration chosen
- chosen on calibration: sufficiency 0.5, selection 0.5
- calls: 32 (32 cached), estimated neurons: 874

## Head to head, held-out cases

| metric | current route | clef |
|---|---:|---:|
| false_answer_rate | 1.0000 | 1.0000 |
| abstention_recall | 0.0000 | 0.0000 |
| coverage | 1.0000 | 0.0000 |
| right_item_selected | 1.0000 | 0.0000 |
| false_selection_per_case | 0.0000 | 0.0000 |
| items_passed | 1.0000 | 0.0000 |
| latency_p50_ms | 0.0000 | 483.0000 |
| latency_p95_ms | 0.0000 | 685.0000 |

## By family, clef

| family | false answers | coverage | right item |
|---|---:|---:|---:|
| gold | 1.0000 | 0.0000 | 0.0000 |
| synthetic | 1.0000 | 0.0000 | 0.0000 |

## Reading the comparison

`false_answer_rate` is the number that decides: answering a question the
corpus cannot answer. `coverage` is the product cost of being strict, and
`items_passed` is the token cost of handing the generator too much.
