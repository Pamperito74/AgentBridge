FROM node:20-alpine AS base
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY tsconfig.json ./
COPY src ./src
COPY public ./public
RUN npm run build

# Production image
FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=4000

# Create data directory and drop to non-root user
RUN mkdir -p /app/data && chown -R node:node /app
USER node

COPY --from=base --chown=node:node /app/package.json /app/package.json
COPY --from=base --chown=node:node /app/node_modules /app/node_modules
COPY --from=base --chown=node:node /app/dist /app/dist
COPY --from=base --chown=node:node /app/public /app/public

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:4000/health || exit 1

CMD ["node", "dist/server/index.js"]
