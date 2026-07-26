<!-- Thanks for contributing! Keep everything in English. -->

## Summary

<!-- What does this PR do and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / internal
- [ ] CI / tooling

## Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `pytest` passes and new code / error paths are covered by tests
- [ ] `radon cc -s -n C hermes_yandex_mail` prints nothing
- [ ] README / docs updated for any user-facing change
- [ ] `imap.py`, `imap_utf7.py`, and `message.py` still have no Hermes imports
- [ ] Tool handlers still return a JSON string on every path, including failures
- [ ] Nothing new can destroy a message: copies precede deletions, expunge stays UID-scoped

## Related issues

<!-- e.g. Closes #123 -->
