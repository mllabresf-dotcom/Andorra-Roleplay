/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ea: {
          primary: "#00aaff",
          bg: "#02040a",
          card: "#101320",
        },
      },
    },
  },
  plugins: [],
};
