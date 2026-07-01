import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { Sidebar } from "@/components/docs/Sidebar";
import { MobileNav } from "@/components/docs/MobileNav";

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Nav />
      <div className="wrap" style={{ maxWidth: 1240 }}>
        <div className="lg:grid lg:grid-cols-[210px_minmax(0,1fr)] lg:gap-12 xl:gap-16">
          {/* desktop sidebar */}
          <aside className="hidden lg:block">
            <div className="sticky top-14 pt-10 pb-16 max-h-[calc(100vh-3.5rem)] overflow-y-auto">
              <Sidebar />
            </div>
          </aside>

          {/* mobile toggle */}
          <div className="lg:hidden pt-6">
            <MobileNav />
          </div>

          <div className="min-w-0 pt-6 lg:pt-10">{children}</div>
        </div>
      </div>
      <Footer />
    </>
  );
}
