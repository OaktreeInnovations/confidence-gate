FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1

ARG NEXT_PUBLIC_API_URL=http://localhost:8001
ARG NEXT_PUBLIC_FIREBASE_API_KEY=fake-api-key
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=qualora-dev.firebaseapp.com
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID=qualora-dev
ARG NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST=http://localhost:9099

ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY
ENV NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ENV NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID
ENV NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST=$NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST

RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 qualora && \
    adduser --system --uid 1001 qualora

COPY --from=builder /app/public ./public
COPY --from=builder --chown=qualora:qualora /app/.next/standalone ./
COPY --from=builder --chown=qualora:qualora /app/.next/static ./.next/static

USER qualora

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
