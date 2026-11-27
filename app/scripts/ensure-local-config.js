'use strict';

const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const target = path.join(root, 'src', 'config.local.ts');
const example = path.join(root, 'src', 'config.local.example.ts');

if (!fs.existsSync(target)) {
  fs.copyFileSync(example, target);
  console.log('Created src/config.local.ts from src/config.local.example.ts');
}
