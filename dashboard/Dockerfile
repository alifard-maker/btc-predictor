FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

RUN mkdir -p public

ARG NEXT_PUBLIC_API_URL=https://btc-predictor-production-f460.up.railway.app
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

EXPOSE 8080

CMD ["sh", "-c", "npm start -- -H 0.0.0.0 -p ${PORT:-8080}"]
