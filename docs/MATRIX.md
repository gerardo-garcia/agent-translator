# Translation matrix

Number of fixtures of each source platform (rows) whose translation to each target (columns) ends with that worst report severity: `info` < `warning` < `lossy` < `manual`. `error` never occurs (the test suite fails on it). Regenerate with `scripts/gen_matrix.py`.

| source (fixtures) \ target | kagent (v1alpha3) | kagent (v1alpha2) | bedrock | bedrock (sdk) | adk | azure (foundry) | azure (agent-framework) | agentspec |
|---|---|---|---|---|---|---|---|---|
| kagent (9) | 4 manual · 2 lossy · 3 info | 1 manual · 5 lossy · 3 info | 9 manual | 9 manual | 1 manual · 1 lossy · 5 warning · 2 info | 8 manual · 1 warning | 2 manual · 2 lossy · 5 info | 8 manual · 1 info |
| bedrock (10) | 10 manual | 10 manual | 6 manual · 4 info | 10 manual | 6 manual · 3 lossy · 1 warning | 10 manual | 10 manual | 10 manual |
| adk (15) | 14 manual · 1 warning | 14 manual · 1 warning | 15 manual | 15 manual | 15 info | 15 manual | 14 manual · 1 info | 7 manual · 4 lossy · 4 info |
| azure (18) | 13 manual · 1 lossy · 4 warning | 13 manual · 1 lossy · 4 warning | 18 manual | 18 manual | 5 manual · 1 lossy · 8 warning · 4 info | 6 manual · 1 lossy · 5 warning · 6 info | 1 manual · 2 lossy · 15 info | 6 manual · 3 lossy · 9 info |
| agentspec (11) | 11 manual | 11 manual | 11 manual | 11 manual | 10 manual · 1 warning | 2 lossy · 9 warning | 1 manual · 2 lossy · 8 info | 11 info |
