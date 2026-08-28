import Link from "next/link";
import { api } from "@/lib/api/client";

export default async function HomePage() {
  let status: string;
  try {
    status = (await api.health()).status;
  } catch (err) {
    status = `unreachable (${(err as Error).message})`;
  }

  return (
    <div>
      <h1>odyssey</h1>
      <p>services/api status: {status}</p>
      <ul>
        <li>
          <Link href="/journeys">Journeys</Link>
        </li>
        <li>
          <Link href="/datasets">Datasets</Link>
        </li>
        <li>
          <Link href="/models">Models</Link>
        </li>
        <li>
          <Link href="/runs">Eval runs</Link>
        </li>
        <li>
          <Link href="/exports">Exports</Link>
        </li>
      </ul>
    </div>
  );
}
