# Decision service report

- service: `http://127.0.0.1:11434`
- model: `tev1:0.8b`
- examples: 500 (data/classifier/test.jsonl)
- local testing runtime only; not a release gate

| metric | result |
|---|---|
| accuracy | 0.8340 |
| macro F1 | 0.8254 |
| question recall | 0.9067 |
| knowledge_update precision | 0.7586 |
| correction precision | 0.8765 |
| precision among confident | 0.9207 |
| coverage (non-ambiguous) | 0.7060 |
| latency p95 | 1838 ms |
