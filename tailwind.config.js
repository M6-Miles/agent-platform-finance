module.exports = {
  content: ['./frontend_prototype.html'],
  theme: {
    extend: {
      colors: {
        sidebar: '#0F172A', primary: '#3B82F6', success: '#10B981',
        warning: '#F59E0B', danger: '#EF4444', surface: '#F8FAFC',
        muted: '#94A3B8', border: '#E2E8F0', dark: '#1E293B'
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['Consolas', 'SFMono-Regular', 'monospace']
      }
    }
  },
  plugins: []
};
