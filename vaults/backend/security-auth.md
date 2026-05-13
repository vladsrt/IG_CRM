---
title: Security & Authentication
created: 2026-05-12
updated: 2026-05-12
tags: [security, jwt, auth, passwords, cors]
---

# Security & Authentication

How the API is secured. This document covers password hashing, JWT tokens, and multi-tenancy enforcement.

---

## Password Hashing

We use **PBKDF2-HMAC-SHA256** from Python's stdlib (`hashlib`). No external dependency needed.

### Storage format

```
pbkdf2_sha256$260000$<salt_hex>$<hash_hex>
```

- **Algorithm**: `pbkdf2_sha256` (self-describing prefix)
- **Iterations**: 260,000 (OWASP recommended minimum for PBKDF2-SHA256)
- **Salt**: 16 random bytes, generated per password
- **Hash**: 32 bytes output

### Verification

`verify_password(plain, stored)` splits the stored string, re-derives the hash with the same salt and iterations, and uses `hmac.compare_digest` for **constant-time comparison** (prevents timing attacks).

### Code location

`app/core/security.py` — functions `hash_password()` and `verify_password()`.

---

## JWT Tokens

### Flow

```
Client                          Server
  │                               │
  │  POST /auth/register          │
  │  { email, password }          │
  │──────────────────────────────►│
  │                               │  hash password, create User + Subscription
  │◄──────────────────────────────│
  │  201 { id, email }            │
  │                               │
  │  POST /auth/login             │
  │  { username, password }       │
  │──────────────────────────────►│
  │                               │  verify password, create JWT
  │◄──────────────────────────────│
  │  200 { access_token, bearer } │
  │                               │
  │  GET /accounts/               │
  │  Authorization: Bearer <jwt>  │
  │──────────────────────────────►│
  │                               │  decode JWT → get user_id → scope query
  │◄──────────────────────────────│
  │  200 [accounts for this user] │
```

### Token structure

The JWT payload (`sub` claim) contains the **user UUID** as a string. No sensitive data is stored in the token.

```json
{
  "sub": "550e8400-e29b-41d4-a716-446655440000",
  "exp": 1715540400
}
```

### Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `SECRET_KEY` | random 64 hex chars | **Must be set in `.env` for production** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 60 | Token lifetime |
| `JWT_ALGORITHM` | HS256 | HMAC-SHA256 signing |

### Code location

- Token creation: `app/core/security.py` → `create_access_token()`
- Token verification: `app/api/dependencies.py` → `get_current_user()`

---

## Multi-tenancy

The system is **multi-tenant by user**. Every data-bearing table has a `user_id` foreign key.

### Enforcement pattern

Protected routes inject `current_user` via FastAPI dependency injection:

```python
@router.get("/")
def list_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = crud_account.list_accounts(db, user_id=current_user.id)
    return [InstagramAccountRead.model_validate(a) for a in rows]
```

The `user_id=current_user.id` parameter scopes the database query to **only return data belonging to the authenticated user**. This prevents IDOR (Insecure Direct Object Reference) attacks.

### What `get_current_user` does

1. Extracts the `Bearer` token from the `Authorization` header.
2. Decodes the JWT and reads the `sub` claim.
3. Looks up the user in Postgres by UUID.
4. Returns the `User` ORM instance or raises `401 Unauthorized`.

---

## CORS

`CORSMiddleware` is configured in `app/api/main.py`:

- **Origins**: pulled from `settings.CORS_ALLOWED_ORIGINS` (defaults to `["*"]` for dev)
- **Methods**: all allowed
- **Headers**: all allowed
- **Credentials**: enabled

> [!WARNING]
> **For production**, set `CORS_ALLOWED_ORIGINS` in `.env` to your actual frontend domain(s). Never leave `*` in production with credentials enabled.

---

## Checklist for adding a protected route

1. Import `get_current_user` from `app.api.dependencies`.
2. Add `current_user: User = Depends(get_current_user)` to the route signature.
3. Pass `user_id=current_user.id` to any CRUD function that supports it.
4. Done — unauthenticated requests get `401`, other users' data is invisible.
