import type { NextConfig } from "next"

// Build ra file tĩnh (out/) để FastAPI của farm-console phục vụ cùng origin với API.
const nextConfig: NextConfig = {
  output: "export",
}

export default nextConfig
