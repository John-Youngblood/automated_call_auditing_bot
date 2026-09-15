# syntax=docker/dockerfile:1

# --- deps: installed separately so code edits do not re-run npm ci ---------
FROM node:24-alpine AS deps
WORKDIR /srv
COPY package.json package-lock.json* ./
# `npm ci` needs a lockfile; fall back to install on a fresh checkout that has
# not generated one yet.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# --- dev: Vite dev server, source bind-mounted at run time ----------------
FROM node:24-alpine AS dev
WORKDIR /srv
ENV NODE_ENV=development
COPY --from=deps /srv/node_modules ./node_modules
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]

# --- build + serve: static bundle behind nginx ----------------------------
FROM deps AS build
COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS prod
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /srv/dist /usr/share/nginx/html
EXPOSE 80
