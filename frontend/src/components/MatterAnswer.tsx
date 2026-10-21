import { buildAnswerView, type AnswerInput, type Notice, type SourceLink } from "./answer-presentation.js";

type OpenSource = (documentId: string, sourceReference: string) => void;

/** A compact source action; the exact file name and reference stay in its accessible name and tooltip. */
function SourceButton({ source, text, onOpenSource }: { source: SourceLink; text?: string; onOpenSource?: OpenSource }) {
  const label = text ?? `${source.label} · ${source.reference}`;
  const description = `${source.filename} · ${source.reference}`;
  if (!onOpenSource) return <span className="source-chip static" title={description}>{label}</span>;
  return <button className="source-chip" type="button" title={description} aria-label={`Open source ${source.reference} in ${source.filename}`}
    onClick={() => onOpenSource(source.documentId, source.reference)}>{label}</button>;
}

function Notices({ notices }: { notices: Notice[] }) {
  if (!notices.length) return null;
  return <ul className="answer-notices" aria-label="Warnings and notes">
    {notices.map((notice) => <li key={notice.text} className={notice.tone === "warning" ? "warning" : "info"}>{notice.text}</li>)}
  </ul>;
}

/**
 * One answer, answer-first: conclusion, structured result, material
 * warnings and legal limitations, then collapsible evidence and technical
 * details.  All document and model text is rendered as React text.
 */
export function MatterAnswer({ input, onOpenSource }: { input: AnswerInput; onOpenSource?: OpenSource }) {
  const view = buildAnswerView(input);
  const holdings = view.holdings;
  const material = view.warnings.filter((notice) => notice.tone === "warning");
  const info = view.warnings.filter((notice) => notice.tone === "info");
  return <div className={`matter-answer layout-${view.layout}`}>
    <header className="answer-header">
      <div>
        <h4>{view.title}</h4>
        {holdings && <p className="answer-subtitle">
          {holdings.earlierDate} → {holdings.laterDate}
          {holdings.investor && <> · Investor: {holdings.investor}</>}
          {holdings.isin && <> · ISIN {holdings.isin}</>}
        </p>}
      </div>
      <span className="answer-badge">{view.method}</span>
    </header>
    <p className="answer-scope">{view.scopeLabel}</p>

    {view.conclusion && <p className="answer-conclusion">{view.conclusion}</p>}
    {view.prose.map((block, index) => block.kind === "paragraph"
      ? <p className="answer-prose" key={index}>{block.text}</p>
      : <ul className="answer-prose" key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}>{item}</li>)}</ul>)}

    {holdings && <>
      <dl className="answer-metrics">
        {holdings.metrics.map((metric) => <div key={metric.label}><dt>{metric.label}</dt><dd>{metric.value}{metric.detail && <small>{metric.detail}</small>}</dd></div>)}
      </dl>
      {holdings.rows.length > 0 && <div className="answer-table" role="region" aria-label="Holdings comparison table" tabIndex={0}><table>
        <caption>{holdings.evidenceRole ? `${holdings.evidenceRole}. ` : ""}Differences are later minus earlier, as recorded in the source cells.</caption>
        <thead><tr className="column-group">
          <td colSpan={2 + (holdings.showInvestorColumn ? 1 : 0) + (holdings.showIsinColumn ? 1 : 0)}></td>
          <th scope="colgroup" colSpan={3} className="numeric">{holdings.quantityLabel}</th>
          <td colSpan={2}></td>
        </tr><tr>
          <th scope="col">Subfund</th>{holdings.showInvestorColumn && <th scope="col">Investor</th>}<th scope="col">Account</th>
          {holdings.showIsinColumn && <th scope="col">ISIN</th>}
          <th scope="col" className="numeric">{holdings.earlierDate}</th><th scope="col" className="numeric">{holdings.laterDate}</th>
          <th scope="col" className="numeric">Recorded difference</th><th scope="col">Currency / unit</th><th scope="col">Evidence</th>
        </tr></thead>
        <tbody>{holdings.rows.map((row) => <tr key={row.key}>
          <th scope="row">{row.subfund}{row.status && <small className="row-status">{row.status}{row.note ? ` — ${row.note}` : ""}</small>}</th>
          {holdings.showInvestorColumn && <td>{row.investor}</td>}
          <td>{row.account}</td>
          {holdings.showIsinColumn && <td>{row.isin}</td>}
          <td className="numeric">{row.earlier.display}</td>
          <td className="numeric">{row.later.display}</td>
          <td className={`numeric difference ${row.difference.direction}`}>{row.difference.display}</td>
          <td>{row.measure}</td>
          <td className="evidence-cell">
            {row.earlier.source && <SourceButton source={row.earlier.source} text={holdings.earlierDate} onOpenSource={onOpenSource} />}
            {row.later.source && <SourceButton source={row.later.source} text={holdings.laterDate} onOpenSource={onOpenSource} />}
          </td>
        </tr>)}</tbody>
        {holdings.totals && <tfoot><tr>
          <th scope="row">{holdings.totals.label}</th>{holdings.showInvestorColumn && <td></td>}<td></td>{holdings.showIsinColumn && <td></td>}
          <td className="numeric">{holdings.totals.earlier}</td><td className="numeric">{holdings.totals.later}</td>
          <td className="numeric difference">{holdings.totals.difference}</td><td>{holdings.totals.measure}</td><td></td>
        </tr></tfoot>}
      </table></div>}
      {!holdings.totals && holdings.totalsNote && holdings.rows.length > 0 && <p className="answer-footnote">{holdings.totalsNote}</p>}
      {holdings.reconciliation.length > 0 && <ul className="answer-reconciliation" aria-label="Control totals">
        {holdings.reconciliation.map((item) => <li key={`${item.source.documentId}:${item.source.reference}`} className={item.state}>
          {item.text} <SourceButton source={item.source} text={item.source.reference.split("!").pop()} onOpenSource={onOpenSource} />
        </li>)}
      </ul>}
    </>}

    {view.entities.length > 0 && <div className="answer-table" role="region" aria-label="Entity listing" tabIndex={0}><table>
      <thead><tr><th scope="col">Entity</th><th scope="col">Role (provisional)</th><th scope="col">Associated in the same record</th><th scope="col">Evidence</th></tr></thead>
      <tbody>{view.entities.map((entity) => <tr key={entity.key}>
        <th scope="row">{entity.name}{entity.detail && <small>{entity.detail}</small>}</th>
        <td>{entity.role}{entity.uncertainty && <small className="row-status">{entity.uncertainty}</small>}</td>
        <td>{entity.related}</td>
        <td className="evidence-cell">{entity.sources.map((source) => <SourceButton key={`${source.documentId}:${source.reference}`} source={source} onOpenSource={onOpenSource} />)}</td>
      </tr>)}</tbody>
    </table></div>}

    {view.relationships.length > 0 && <div className="answer-table" role="region" aria-label="Entity relationships" tabIndex={0}><table>
      <thead><tr><th scope="col">Subfund / vehicle</th><th scope="col">Associated entity</th><th scope="col">Basis</th><th scope="col">Evidence</th></tr></thead>
      <tbody>{view.relationships.map((relation) => <tr key={relation.key}>
        <th scope="row">{relation.subject}<small>{relation.subjectRole}</small></th>
        <td>{relation.related}<small>{relation.relatedRole}</small></td>
        <td className={relation.documented ? "" : "muted"}>{relation.basis}</td>
        <td className="evidence-cell">{relation.sources.map((source) => <SourceButton key={source.reference} source={source} onOpenSource={onOpenSource} />)}</td>
      </tr>)}</tbody>
    </table></div>}

    <Notices notices={material} />

    {(view.limitations.length > 0 || view.statedCaveats.length > 0) && <section className="answer-limitations" aria-label="Legal limitations">
      <p className="section-label">Legal limitations</p>
      <ul>{view.limitations.map((text) => <li key={text}>{text}</li>)}</ul>
      {view.statedCaveats.map((caveat) => <p className="stated-caveat" key={caveat.text}>
        <span>Stated in the source: “{caveat.text}”</span>
        <span className="caveat-sources">{caveat.sources.map((source) => <SourceButton key={`${source.documentId}:${source.reference}`} source={source} onOpenSource={onOpenSource} />)}</span>
      </p>)}
    </section>}

    <Notices notices={info} />

    {view.evidence.length > 0 && <details className="answer-details">
      <summary>Supporting evidence · {view.evidence.reduce((total, document) => total + document.count, 0)} references in {view.evidence.length} document{view.evidence.length === 1 ? "" : "s"}</summary>
      {view.evidence.map((document) => {
        const name = <><strong>{document.label}</strong>{document.label !== document.filename && <small>{document.filename}</small>}<span className="evidence-count">{document.count} reference{document.count === 1 ? "" : "s"}</span></>;
        const groups = document.groups.map((group) => <div className="evidence-group" key={group.label}>
          <p className="evidence-group-label">{group.label}</p>
          <ul>{group.items.map((item) => <li key={item.reference}>
            <SourceButton source={{ documentId: document.documentId, filename: document.filename, reference: item.reference, label: document.label }} text={item.shortReference} onOpenSource={onOpenSource} />
            <span className="evidence-snippet">{item.snippet}</span>
          </li>)}</ul>
        </div>);
        // A long list stays collapsed per document when several documents are cited.
        return view.evidence.length > 1 && document.count > 6
          ? <details className="evidence-document" key={document.documentId}><summary className="evidence-document-name">{name}</summary>{groups}</details>
          : <div className="evidence-document" key={document.documentId}><p className="evidence-document-name">{name}</p>{groups}</div>;
      })}
    </details>}

    <details className="answer-details technical">
      <summary>Technical details</summary>
      <dl>{view.technical.map((detail, index) => <div key={`${detail.label}:${index}`}><dt>{detail.label}</dt><dd>{detail.value}</dd></div>)}</dl>
    </details>
  </div>;
}

/** Shown in the transcript while a submitted question is being answered. */
export function PendingAnswer({ question, followUp = false }: { question: string; followUp?: boolean }) {
  return <article className="chat-turn pending" aria-busy="true">
    <div className="chat-question"><p className="section-label">{followUp ? "Follow-up question" : "Your question"}</p><p>{question}</p></div>
    <p className="chat-pending" role="status"><span className="loading-spinner" aria-hidden="true" />Preparing a cited answer…</p>
  </article>;
}

/** Shown in the transcript when a submitted question could not be answered. */
export function FailedAnswer({ question, message, followUp = false }: { question: string; message: string; followUp?: boolean }) {
  return <article className="chat-turn failed">
    <div className="chat-question"><p className="section-label">{followUp ? "Follow-up question" : "Your question"}</p><p>{question}</p></div>
    <p className="viewer-error" role="alert">{message}</p>
  </article>;
}
