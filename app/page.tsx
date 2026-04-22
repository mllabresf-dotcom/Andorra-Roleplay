export default function Home() {
  return (
    <main className="min-h-screen relative">
      <div className="fixed inset-0 -z-20 overflow-hidden">
        <img
          src="/bg.jpg"
          alt="Background"
          className="w-full h-full object-cover brightness-75 scale-105"
        />
        <div className="absolute inset-0 bg-gradient-to-b from-black/70 via-black/80 to-black/95" />
      </div>

      <section className="min-h-screen flex flex-col items-center justify-center px-4 text-center">
        <header className="mb-4">
          <img
            src="/Andorra_Roleplay_Logo.png"
            alt="Emergency Andorra"
            className="max-h-24 drop-shadow-xl"
          />
        </header>
        <h1 className="text-3xl md:text-5xl font-semibold drop-shadow-2xl">
          Welcome to Emergency Andorra
        </h1>
        <div className="inline-flex items-center gap-2 mt-3 px-4 py-2 rounded-full bg-black/50 backdrop-blur border border-white/10 text-sm md:text-base">
          <span>Your fun starts here!</span>
          <span className="opacity-70 font-bold">·</span>
          <span>¡Tu diversión comienza aquí!</span>
        </div>
        <div className="flex flex-wrap gap-3 justify-center mt-5">
          <a className="px-5 py-3 rounded-xl border border-ea-primary bg-black/70 hover:bg-ea-primary hover:text-black transition">
            Play Now
          </a>
          <a className="px-5 py-3 rounded-xl border border-ea-primary bg-black/70 hover:bg-ea-primary hover:text-black transition">
            Discord
          </a>
        </div>
      </section>
    </main>
  );
}
