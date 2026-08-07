"""Reference content for migrations to seed — data only, no SQLAlchemy models
and no `app.*` imports. Deliberately its own top-level package (a sibling of
`app/`, not nested under `alembic/`, which would collide with the installed
`alembic` package name on import) so it stays reachable from both migrations
and tests without pulling either into the other's dependency graph.

A migration that has already shipped (been applied anywhere outside local
dev) does not get retroactively edited to import from here — see each
migration's own comments for which ones predate this package. A new
migration is free to import from here; a *content* change (a new milestone
type, a corrected offset) is a new insert or a new migration, per the same
"never renamed/edited once referenced" discipline `ontology_terms.code`
already documents — this package is versioned in git precisely so that
discipline is reviewable as an ordinary diff.
"""
