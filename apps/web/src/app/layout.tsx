import type { Metadata } from "next";
import { Sidebar } from "@/components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "odyssey",
  description: "journeys, datasets, models, eval runs, exports",
};

// Every page here reads live data from services/api, so render the whole
// app dynamically by default rather than opting each page in individually.
// Route segment config inherits down from a layout unless a page overrides
// it, so this covers current and future pages under app/.
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Sidebar />
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
