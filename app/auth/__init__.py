"""Who is calling, and what they are allowed to be.

§19 wants authentication; §20's audit wants an *actor*, which does not exist anywhere in
`app/` yet. This package is where both come from, and it is deliberately separate from `api/`:
the identity of a caller is not a routing concern, and §20 will record it from the pipeline
rather than from a request handler.

Modes arrive one at a time (#128). This holds the model they all resolve to.
"""
