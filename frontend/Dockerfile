FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1

ARG NEXT_PUBLIC_FIREBASE_API_KEY=fake-api-key
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID=your-project-id
ARG NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST=http://localhost:9099
ARG BACKEND_URL=http://localhost:8001

ENV NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY
ENV NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ENV NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID
ENV NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST=$NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST
ENV BACKEND_URL=$BACKEND_URL

RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 cg && \
    adduser --system --uid 1001 cg

COPY --from=builder /app/public ./public
COPY --from=builder --chown=cg:cg /app/.next/standalone ./
COPY --from=builder --chown=cg:cg /app/.next/static ./.next/static

USER cg

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
