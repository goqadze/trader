# --- Stage 1: build the React app with Vite ---
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build          # outputs static files to /app/dist

# --- Stage 2: serve the built files with nginx (also proxies API + WebSocket) ---
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
