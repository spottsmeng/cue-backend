import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, Timestamped, TZDateTime, UUIDPk

# FR-ADM-01's role list is a fixed, closed set — who CUE's own authorization
# logic knows how to check, not extensible domain vocabulary a tenant could
# add to. Same reasoning app/models/ledger.py's CommitmentState comment gives
# for staying a native enum rather than an ontology_terms row: this is a
# structural vocabulary the application code branches on (see
# app/identity/service.py's WRITE_ROLES), not reference data a project would
# ever need to extend.
MembershipRole = Enum(
    "project_manager", "producer", "finance", "account_manager", "designer",
    "administrator", "delegate", "read_only",
    name="membership_role",
)


class User(Base, UUIDPk, Timestamped):
    """PRD §4.1: USER ||--o{ MEMBERSHIP / DELEGATION / VERIFICATION.

    Org-scoped, not global — CUE is multi-tenant SaaS (CUE-Tech-Stack.md
    §2.6) and each organisation federates its own IdP tenant, so the same
    external identity at two different employers is two distinct rows here,
    same as `parties` is org-scoped per contact. Rows are upserted from
    verified JWT claims (app/identity/service.py's resolve_user), never
    created by a caller-supplied id — there is no direct-create endpoint.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("issuer", "external_subject", name="users_issuer_subject_key"),
        UniqueConstraint("organisation_id", "email", name="users_org_email_key"),
    )

    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id"), index=True
    )
    issuer: Mapped[str] = mapped_column(comment="token 'iss' claim — which provider vouched for this user")
    external_subject: Mapped[str] = mapped_column(comment="token 'sub' claim — stable per issuer")
    email: Mapped[str] = mapped_column()
    display_name: Mapped[str | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True)


class Membership(Base, UUIDPk):
    """PRD §4.1: PROJECT ||--o{ MEMBERSHIP : grants. FR-ADM-02's project-scoped
    access — a user's presence here (any role) is what makes a project visible
    to them at the application level, on top of (not instead of) RLS's
    org-level isolation. One row per (user, project); a role change updates
    the row rather than appending a new one — FR-ADM-04's audit requirement is
    scoped to *delegation* grants specifically, not standing membership, so
    this table doesn't need delegations.py's append-only trigger.
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="memberships_user_project_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    role: Mapped[str] = mapped_column(MembershipRole)
    granted_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )


class Delegation(Base, UUIDPk):
    """FR-ADM-03: time-boxed delegation — `delegate_id` temporarily gains
    `role`'s access on `project_id`, until `expires_at` (or early
    `revoked_at`). Checked at request time (app/identity/service.py's
    effective_roles filters `expires_at > now() AND revoked_at IS NULL`), not
    a scheduled sweep — expiry is then always consistent with "now" and
    doesn't depend on a background job having run; the tradeoff is an
    unenforced-but-harmless row lingering in the table after expiry, which is
    fine since nothing else reads this table without that same filter.

    This table *is* FR-ADM-04's audit trail, not a separate log of it — see
    the migration for the DB trigger that makes it append-only except for the
    one legitimate mutation (revocation, via revoked_at/revoked_by). "Who
    granted what to whom, when" is granted_by/delegate_id/role/granted_at,
    permanent from insert; "when it expired" is expires_at (planned) or
    revoked_at (early, distinct from a natural expiry). A second table
    recording the same facts would just be this one, worse.
    """

    __tablename__ = "delegations"
    __table_args__ = (
        CheckConstraint("delegate_id != delegator_id", name="delegation_not_self"),
        CheckConstraint("expires_at > granted_at", name="delegation_expires_after_grant"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    delegator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True, comment="whose access is being lent"
    )
    delegate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True, comment="who temporarily receives it"
    )
    role: Mapped[str] = mapped_column(MembershipRole, comment="the role's access being delegated")
    granted_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    granted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), comment="who authorised the grant — delegator or an administrator"
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), default=None
    )
