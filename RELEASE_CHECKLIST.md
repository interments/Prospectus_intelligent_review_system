# Release Checklist (Open-Source Minimal)

## 1) Repository hygiene
- [ ] No `.venv/`, `node_modules/`, `artifacts/`, `uploads/`
- [ ] No secrets in code or docs (`API_KEY`, tokens, private URLs)
- [ ] `.gitignore` covers runtime/generated files

## 2) Run validation
- [ ] Backend starts: `python -m app.server`
- [ ] Frontend starts: `npm run dev`
- [ ] Health check: `GET /api/v1/health`
- [ ] One task can be created and completed

## 3) Config consistency
- [ ] `README.md` required envs match `backend/.env.example`
- [ ] Optional envs documented (Redis / runtime)

## 4) UX sanity
- [ ] More menu popup works and is visible above all blocks
- [ ] Module tooltip displays correctly
- [ ] Dark mode and light mode both readable

## 5) Documentation
- [ ] README has stack, structure, quickstart
- [ ] UI screenshots in `imgs/` render correctly in README
