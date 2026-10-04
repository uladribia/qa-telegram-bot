# Clef-Flash post-retrieval decisions

- revision: `28096cbf79389c9ccf0627fcaf545eb3081c15ba`
- model: @cf/cloudflare/clef-flash, one request per case, no fallback
- cases: held-out scored, calibration chosen
- chosen on calibration: sufficiency 0.5, selection 0.9
- calls: 32 (0 cached), estimated neurons: 874

## Head to head, held-out cases

| metric | current route | clef |
|---|---:|---:|
| false_answer_rate | 1.0000 | 0.0000 |
| abstention_recall | 0.0000 | 1.0000 |
| coverage | 1.0000 | 0.9130 |
| right_item_selected | 1.0000 | 1.0000 |
| false_selection_per_case | 0.0000 | 0.0000 |
| items_passed | 1.0000 | 0.1134 |
| latency_p50_ms | 0.0000 | 284.0000 |
| latency_p95_ms | 0.0000 | 588.0000 |

## By family, clef

| family | false answers | coverage | right item |
|---|---:|---:|---:|
| gold | 0.0000 | 0.8788 | 1.0000 |
| synthetic | 0.0000 | 0.9322 | 1.0000 |

## Reading the comparison

`false_answer_rate` is the number that decides: answering a question the
corpus cannot answer. `coverage` is the product cost of being strict, and
`items_passed` is the token cost of handing the generator too much.

The reference column is the retrieval-side of today's route, which has no
abstention of its own: anything cosine put in the shortlist is answered, and
the generator alone may decline. That is not the whole deployed behaviour —
the generator abstains on 98.7% of unknown questions — but the generator's
declines are a property of a 35 s-class text model reading a prompt, not a
decision about whether the evidence is sufficient.
