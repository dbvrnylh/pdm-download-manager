// ESLint (configuration « flat ») : règles de base uniquement, aucune dépendance à installer.
export default [
  {
    files: ['extension/**/*.js'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: {
        chrome: 'readonly',
        crypto: 'readonly',
        document: 'readonly',
        window: 'readonly',
        navigator: 'readonly',
        location: 'readonly',
        console: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        setInterval: 'readonly',
        requestAnimationFrame: 'readonly',
        HTMLVideoElement: 'readonly',
        Intl: 'readonly',
        URL: 'readonly',
      },
    },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': 'error',
      'no-unreachable': 'error',
      'no-dupe-keys': 'error',
      'no-fallthrough': 'error',
      'no-constant-condition': 'error',
      'no-control-regex': 'error',
      'prefer-const': 'error',
      'require-await': 'warn',
      // Une constante utilisée avant sa déclaration plante au chargement du module.
      'no-use-before-define': ['error', { functions: false, classes: true, variables: true }],
    },
  },
];
