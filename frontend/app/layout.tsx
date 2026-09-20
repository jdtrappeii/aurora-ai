import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Aurora AI",
  description: "Profitability intelligence for any business",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
