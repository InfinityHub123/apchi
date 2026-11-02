# HTTP API

Generated from the committed `openapi.json`, which CI regenerates and fails on a diff -- so
this is the contract itself and not a description of it. Every endpoint is versioned under
`/api/v1`; §19 has the reasoning, including why `/admin/*` is a separate prefix rather than a
flag on each route.

<swagger-ui src="openapi.json"/>
