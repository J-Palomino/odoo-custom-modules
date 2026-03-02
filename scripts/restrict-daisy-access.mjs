#!/usr/bin/env node
/**
 * Restrict Daisy Agency module access to Juan only.
 *
 * 1. Finds the three daisydo_agents security groups
 * 2. Removes all users from those groups
 * 3. Assigns group_daisy_admin (which implies officer + user) to Juan
 *
 * Usage: ODOO_API_KEY=xxx node scripts/restrict-daisy-access.mjs
 */
import 'dotenv/config';

const ODOO_URL = process.env.PUBLIC_ODOO_URL || 'https://letsgomint.us';
const ODOO_DB = process.env.ODOO_DB || 'odoo';
const ODOO_USERNAME = process.env.ODOO_USERNAME || 'jpalomino@brightroot.com';
const ODOO_API_KEY = process.env.ODOO_API_KEY;

if (!ODOO_API_KEY) { console.error('ODOO_API_KEY not set'); process.exit(1); }

let uid, reqId = 0;

async function jsonrpc(service, method, args) {
  const res = await fetch(`${ODOO_URL}/jsonrpc`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'call', id: ++reqId, params: { service, method, args } }),
  });
  const data = await res.json();
  if (data.error) throw new Error(data.error.data?.message || data.error.message);
  return data.result;
}

async function execute(model, method, args, kwargs = {}) {
  return jsonrpc('object', 'execute_kw', [ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs]);
}

async function main() {
  console.log(`Authenticating to ${ODOO_URL}...`);
  uid = await jsonrpc('common', 'authenticate', [ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {}]);
  console.log(`UID: ${uid}`);

  // Step 1: Find Juan's user
  console.log('\n1. Finding Juan...');
  const juanUsers = await execute('res.users', 'search_read', [
    [['login', 'ilike', 'jpalomino']]
  ], { fields: ['id', 'name', 'login', 'groups_id'], limit: 5 });

  if (juanUsers.length === 0) {
    // Try broader search
    const allJuans = await execute('res.users', 'search_read', [
      [['name', 'ilike', 'juan']]
    ], { fields: ['id', 'name', 'login'], limit: 10 });
    console.log('Users matching "juan":', allJuans.map(u => `${u.name} <${u.login}> (id=${u.id})`));
    console.error('Could not find Juan by login "jpalomino". Please check and adjust.');
    process.exit(1);
  }

  const juan = juanUsers[0];
  console.log(`Found: ${juan.name} <${juan.login}> (id=${juan.id})`);

  // Step 2: Find the Daisy Agency groups
  console.log('\n2. Finding Daisy Agency groups...');
  const groups = await execute('res.groups', 'search_read', [
    [['name', 'in', ['User', 'Officer', 'Administrator']],
     ['privilege_id.name', '=', 'Daisy Agency']]
  ], { fields: ['id', 'name', 'users'] });

  if (groups.length === 0) {
    // Fallback: search by category/module
    const groupsFallback = await execute('res.groups', 'search_read', [
      [['full_name', 'ilike', 'daisy agency']]
    ], { fields: ['id', 'name', 'full_name', 'users'] });
    console.log('Groups found by fallback:', groupsFallback.map(g => `${g.full_name} (id=${g.id}, users=${g.users.length})`));

    if (groupsFallback.length === 0) {
      console.error('No Daisy Agency groups found. Is the module installed?');
      process.exit(1);
    }
    groups.push(...groupsFallback);
  }

  for (const g of groups) {
    console.log(`  ${g.name} (id=${g.id}): ${g.users.length} users`);
  }

  // Step 3: Remove all users from all Daisy Agency groups
  console.log('\n3. Removing all users from Daisy Agency groups...');
  for (const g of groups) {
    if (g.users.length > 0) {
      // [(5, 0, 0)] = clear all links
      await execute('res.groups', 'write', [[g.id], { users: [[5, 0, 0]] }]);
      console.log(`  Cleared ${g.users.length} users from "${g.name}"`);
    }
  }

  // Step 4: Add Juan to the Administrator group (implies Officer + User)
  console.log('\n4. Assigning Daisy Agency Administrator to Juan...');
  const adminGroup = groups.find(g => g.name === 'Administrator');
  if (!adminGroup) {
    console.error('Administrator group not found in results:', groups.map(g => g.name));
    process.exit(1);
  }

  // [(4, juan.id)] = add link
  await execute('res.groups', 'write', [[adminGroup.id], { users: [[4, juan.id]] }]);
  console.log(`  Added ${juan.name} to "${adminGroup.name}" (id=${adminGroup.id})`);

  // Step 5: Verify
  console.log('\n5. Verifying...');
  const updated = await execute('res.groups', 'search_read', [
    [['id', 'in', groups.map(g => g.id)]]
  ], { fields: ['id', 'name', 'users'] });

  for (const g of updated) {
    const userNames = g.users.length > 0
      ? await execute('res.users', 'read', [g.users], { fields: ['name', 'login'] })
      : [];
    console.log(`  ${g.name}: ${userNames.map(u => `${u.name} <${u.login}>`).join(', ') || '(none)'}`);
  }

  console.log('\nDone! Only Juan has Daisy Agency access.');
}

main().catch(err => { console.error('FATAL:', err.message); process.exit(1); });
