const apiOrigin = (process.env.DAEMONCUT_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  allowedDevOrigins: ["localhost", "127.0.0.1", "*.e2b.app"],
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/bridge", destination: `${apiOrigin}/bridge` },
      { source: "/bridge/:path*", destination: `${apiOrigin}/bridge/:path*` },
      { source: "/ws/:path*", destination: `${apiOrigin}/ws/:path*` }
    ];
  }
};

export default nextConfig;
