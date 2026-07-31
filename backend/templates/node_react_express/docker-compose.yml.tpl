services:
  app:
    build: .
    environment:
      PORT: "{{PORT}}"
      JWT_SECRET: "${JWT_SECRET:?JWT_SECRET is required}"
    ports:
      - "{{PORT}}:{{PORT}}"
