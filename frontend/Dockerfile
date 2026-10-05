# syntax=docker/dockerfile:1
#
# ResolveIQ reviewer workbench — multi-stage build.
#
# Stage 1 builds the bundle; stage 2 serves it from nginx. The build stage never
# reaches the final image, so no npm cache, source map or toolchain is shipped.
#
# No build-time secrets. `npm run build` runs `tsc --noEmit` before Vite, and Vite
# inlines VITE_* variables into the bundle — so no LLM key may ever be given a
# VITE_ prefix or it would end up readable in the browser (see NEP-09). The API
# client uses a relative `/api/v1`, so nothing host-specific is baked in at all.

FROM node:22-alpine AS build

WORKDIR /app

# Dependencies first so source edits do not invalidate the npm layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json vite.config.ts index.html postcss.config.js tailwind.config.js eslint.config.js ./
COPY src ./src

RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]