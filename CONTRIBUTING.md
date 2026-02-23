# 🚀 Contributing to LivePulse

First off, thank you for considering contributing to LivePulse! 🎉 
This project is built by a dedicated team, and we want to ensure that our collaborative process is smooth, efficient, and fun. 

## 📚 Table of Contents
1. [Code of Conduct](#-code-of-conduct)
2. [Branching Strategy](#-branching-strategy)
3. [Commit Guidelines](#-commit-guidelines)
4. [Local Development Setup](#-local-development-setup)
5. [Pull Request Process](#-pull-request-process)

## 🤝 Code of Conduct
By participating in this project, you agree to abide by our Code of Conduct. We expect all team members to be respectful, constructive, and supportive of each other's learning process.

## 🌿 Branching Strategy
To keep our `main` and `dev` branches clean, we use a strict role-based branching system. **Never commit directly to `main` or `dev`.**

Create your feature branches off the `dev` branch using the following prefixes:
* 🎨 **Frontend:** `frontend/<feature-name>` (e.g., `frontend/voting-buttons`)
* ⚙️ **Backend:** `backend/<feature-name>` (e.g., `backend/redis-api`)
* 🗄️ **Database:** `db/<feature-name>` (e.g., `db/mysql-schema`)

## 💬 Commit Guidelines
We follow Conventional Commits to keep our history readable. Start your commit messages with one of these emojis/tags:
* `✨ feat:` A new feature
* `🐛 fix:` A bug fix
* `📝 docs:` Documentation changes
* `♻️ refactor:` Code changes that neither fix a bug nor add a feature
* `🚀 deploy:` Deployment or configuration changes

*Example:* `✨ feat: added Redis connection pool to server.py`

## 🛠️ Local Development Setup
Before you write code, ensure your local environment matches the team's:
1. Clone the repo and switch to the `dev` branch.
2. Ensure **Redis** is running on `localhost:6379`.
3. Create a Python virtual environment and run: `pip install -r requirements.txt`.
4. Never push your `.env` files or virtual environments to GitHub!

## 🔄 Pull Request Process
1. Push your branch: `git push origin your-branch-name`.
2. Open a PR against the `dev` branch.
3. Fill out the provided PR template completely.
4. Request a review from at least one other team member.
5. Once approved and all merge conflicts are resolved, squash and merge!
